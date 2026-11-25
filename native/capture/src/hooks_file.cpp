// Hooks on kernel32/kernelbase: COM ports and named pipes (serial transport), HID
// device handles, overlapped completion paths, serial line settings, and the loader.
//
// Behavior per hook (documented in docs/hooks.md):
//   CreateFileW/A        track COM, \\.\pipe\, HID and WinUSB device paths that pass the filter
//   CloseHandle          close event for tracked handles
//   ReadFile/WriteFile   data events; pending overlapped reads are remembered
//   ReadFileEx/WriteFileEx  same, with the completion routine wrapped
//   GetOverlappedResult(Ex), GetQueuedCompletionStatus(Ex)  complete pending reads
//   WaitCommEvent        signal event with the event mask
//   DeviceIoControl      serial IOCTLs decoded into config/signal events
//   SetCommState, SetCommTimeouts, EscapeCommFunction  decoded config/signal events
//   LoadLibrary(Ex)A/W   attach VISA/FTDI/WinUSB/HID/Winsock hooks when those DLLs load
//   GetProcAddress       the same, as a safety net before the app resolves a function
#include <algorithm>
#include <cwctype>
#include <string>

#include "common.h"

namespace calder {
namespace {

decltype(&CreateFileW) Real_CreateFileW = CreateFileW;
decltype(&CreateFileA) Real_CreateFileA = CreateFileA;
decltype(&CloseHandle) Real_CloseHandle = CloseHandle;
decltype(&ReadFile) Real_ReadFile = ReadFile;
decltype(&WriteFile) Real_WriteFile = WriteFile;
decltype(&ReadFileEx) Real_ReadFileEx = ReadFileEx;
decltype(&WriteFileEx) Real_WriteFileEx = WriteFileEx;
decltype(&GetOverlappedResult) Real_GetOverlappedResult = GetOverlappedResult;
decltype(&GetOverlappedResultEx) Real_GetOverlappedResultEx = GetOverlappedResultEx;
decltype(&GetQueuedCompletionStatus) Real_GetQueuedCompletionStatus = GetQueuedCompletionStatus;
decltype(&GetQueuedCompletionStatusEx) Real_GetQueuedCompletionStatusEx = GetQueuedCompletionStatusEx;
decltype(&WaitCommEvent) Real_WaitCommEvent = WaitCommEvent;
decltype(&DeviceIoControl) Real_DeviceIoControl = DeviceIoControl;
decltype(&SetCommState) Real_SetCommState = SetCommState;
decltype(&SetCommTimeouts) Real_SetCommTimeouts = SetCommTimeouts;
decltype(&EscapeCommFunction) Real_EscapeCommFunction = EscapeCommFunction;
decltype(&LoadLibraryExW) Real_LoadLibraryExW = LoadLibraryExW;
decltype(&LoadLibraryExA) Real_LoadLibraryExA = LoadLibraryExA;
decltype(&LoadLibraryW) Real_LoadLibraryW = LoadLibraryW;
decltype(&LoadLibraryA) Real_LoadLibraryA = LoadLibraryA;
decltype(&GetProcAddress) Real_GetProcAddress = GetProcAddress;

// Device paths of WinUSB devices, until WinUsb_Initialize turns them into interfaces.
std::mutex g_usb_mutex;
std::unordered_map<std::uint64_t, std::string> g_usb_paths;

std::uint64_t Key(HANDLE h) { return reinterpret_cast<std::uint64_t>(h); }

std::wstring Lower(const std::wstring& s) {
    std::wstring out(s);
    std::transform(out.begin(), out.end(), out.begin(), [](wchar_t c) { return static_cast<wchar_t>(std::towlower(c)); });
    return out;
}

// Maps a CreateFile path to a transport and a resource identity.
Transport Classify(const std::wstring& path, std::string* identity) {
    const std::wstring low = Lower(path);
    if (low.find(L"hid#") != std::wstring::npos) {
        *identity = "hid:" + Utf8(path.c_str());
        return Transport::Hid;
    }
    if (low.find(L"usb#vid_") != std::wstring::npos) {
        *identity = "winusb:" + Utf8(path.c_str());
        return Transport::WinUsb;
    }
    std::wstring tail = low;
    for (const wchar_t* prefix : {L"\\\\.\\", L"\\\\?\\"}) {
        if (tail.rfind(prefix, 0) == 0) tail = tail.substr(4);
    }
    if (tail.size() > 3 && tail.rfind(L"com", 0) == 0 &&
        std::all_of(tail.begin() + 3, tail.end(), [](wchar_t c) { return std::iswdigit(c) != 0; })) {
        std::string port = Utf8(tail.c_str());
        std::transform(port.begin(), port.end(), port.begin(), [](char c) { return static_cast<char>(std::toupper(c)); });
        *identity = "serial:" + port;
        return Transport::Serial;
    }
    if (low.rfind(L"\\\\.\\pipe\\", 0) == 0) {
        *identity = "serial:" + Utf8(path.c_str());
        return Transport::Serial;
    }
    return Transport::None;
}

void TrackCreate(HANDLE h, const wchar_t* path) {
    if (h == INVALID_HANDLE_VALUE || path == nullptr) return;
    std::string identity;
    const Transport t = Classify(path, &identity);
    if (t == Transport::None || !Allowed(t, identity)) return;
    LastErrorGuard guard;
    if (t == Transport::WinUsb) {
        std::lock_guard lock(g_usb_mutex);
        g_usb_paths[Key(h)] = Utf8(path);
        return;
    }
    Json meta;
    meta.wstr("path", path).str("identity", identity);
    if (t == Transport::Serial && Lower(path).find(L"\\pipe\\") != std::wstring::npos) meta.str("device_kind", "pipe");
    EmitOpen(t, Key(h), identity, meta.done());
}

HANDLE WINAPI Mine_CreateFileW(LPCWSTR name, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES sa, DWORD disposition,
                               DWORD flags, HANDLE templ) {
    HANDLE h = Real_CreateFileW(name, access, share, sa, disposition, flags, templ);
    TrackCreate(h, name);
    return h;
}

HANDLE WINAPI Mine_CreateFileA(LPCSTR name, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES sa, DWORD disposition,
                               DWORD flags, HANDLE templ) {
    HANDLE h = Real_CreateFileA(name, access, share, sa, disposition, flags, templ);
    if (h != INVALID_HANDLE_VALUE && name != nullptr) {
        LastErrorGuard guard;
        wchar_t wide[MAX_PATH * 2] = {};
        if (MultiByteToWideChar(CP_ACP, 0, name, -1, wide, MAX_PATH * 2) > 0) TrackCreate(h, wide);
    }
    return h;
}

BOOL WINAPI Mine_CloseHandle(HANDLE h) {
    Tracked info;
    const bool tracked = Handles().Remove(Key(h), &info);
    BOOL ok = Real_CloseHandle(h);
    if (tracked) {
        LastErrorGuard guard;
        EmitClose(info.transport, Key(h));
    }
    {
        std::lock_guard lock(g_usb_mutex);
        g_usb_paths.erase(Key(h));
    }
    return ok;
}

void CompleteRead(const OVERLAPPED* ov, DWORD bytes) {
    PendingRead pending;
    if (ov == nullptr || !Pending().Take(ov, &pending)) return;
    if (pending.buffer != nullptr) {
        EmitData(pending.transport, pending.handle, Direction::Rx, pending.buffer, std::min<DWORD>(bytes, pending.capacity));
        return;
    }
    // WSARecv scatter buffers.
    DWORD left = bytes;
    for (const WSABUF& b : pending.wsabufs) {
        if (left == 0) break;
        const DWORD n = std::min<DWORD>(left, b.len);
        EmitData(pending.transport, pending.handle, Direction::Rx, b.buf, n);
        left -= n;
    }
}

BOOL WINAPI Mine_ReadFile(HANDLE h, LPVOID buffer, DWORD toRead, LPDWORD read, LPOVERLAPPED ov) {
    Tracked info;
    if (!Handles().Find(Key(h), &info) || Suppress::Active()) return Real_ReadFile(h, buffer, toRead, read, ov);
    if (ov != nullptr) {
        PendingRead p;
        p.handle = Key(h);
        p.transport = info.transport;
        p.buffer = buffer;
        p.capacity = toRead;
        Pending().Add(ov, p);
    }
    BOOL ok = Real_ReadFile(h, buffer, toRead, read, ov);
    LastErrorGuard guard;
    const DWORD err = GetLastError();
    if (ok) {
        if (ov == nullptr) {
            if (read != nullptr) EmitData(info.transport, Key(h), Direction::Rx, buffer, *read);
        } else {
            // Completed synchronously; a completion packet may still follow, so keep
            // only one report: log now and forget the pending entry.
            PendingRead p;
            if (Pending().Take(ov, &p)) {
                const DWORD n = read != nullptr ? *read : static_cast<DWORD>(ov->InternalHigh);
                EmitData(info.transport, Key(h), Direction::Rx, buffer, n);
            }
        }
    } else if (ov != nullptr && err != ERROR_IO_PENDING) {
        Pending().Take(ov, nullptr);
    }
    return ok;
}

BOOL WINAPI Mine_WriteFile(HANDLE h, LPCVOID buffer, DWORD toWrite, LPDWORD written, LPOVERLAPPED ov) {
    BOOL ok = Real_WriteFile(h, buffer, toWrite, written, ov);
    Tracked info;
    if (!Suppress::Active() && Handles().Find(Key(h), &info)) {
        LastErrorGuard guard;
        const DWORD err = GetLastError();
        if (ok || (ov != nullptr && err == ERROR_IO_PENDING)) {
            const DWORD n = (ok && ov == nullptr && written != nullptr) ? *written : toWrite;
            EmitData(info.transport, Key(h), Direction::Tx, buffer, n);
        }
    }
    return ok;
}

void CALLBACK ReadExCompletion(DWORD error, DWORD bytes, LPOVERLAPPED ov) {
    PendingRead pending;
    LPOVERLAPPED_COMPLETION_ROUTINE user = nullptr;
    if (Pending().Take(ov, &pending)) {
        user = pending.user_routine;
        if (error == 0 && pending.buffer != nullptr) {
            EmitData(pending.transport, pending.handle, Direction::Rx, pending.buffer, std::min<DWORD>(bytes, pending.capacity));
        }
    }
    if (user != nullptr) user(error, bytes, ov);
}

BOOL WINAPI Mine_ReadFileEx(HANDLE h, LPVOID buffer, DWORD toRead, LPOVERLAPPED ov, LPOVERLAPPED_COMPLETION_ROUTINE routine) {
    Tracked info;
    if (!Handles().Find(Key(h), &info) || ov == nullptr || routine == nullptr) {
        return Real_ReadFileEx(h, buffer, toRead, ov, routine);
    }
    PendingRead p;
    p.handle = Key(h);
    p.transport = info.transport;
    p.buffer = buffer;
    p.capacity = toRead;
    p.user_routine = routine;
    Pending().Add(ov, p);
    BOOL ok = Real_ReadFileEx(h, buffer, toRead, ov, ReadExCompletion);
    if (!ok) {
        LastErrorGuard guard;
        Pending().Take(ov, nullptr);
    }
    return ok;
}

BOOL WINAPI Mine_WriteFileEx(HANDLE h, LPCVOID buffer, DWORD toWrite, LPOVERLAPPED ov, LPOVERLAPPED_COMPLETION_ROUTINE routine) {
    BOOL ok = Real_WriteFileEx(h, buffer, toWrite, ov, routine);
    Tracked info;
    if (ok && Handles().Find(Key(h), &info)) {
        LastErrorGuard guard;
        EmitData(info.transport, Key(h), Direction::Tx, buffer, toWrite);
    }
    return ok;
}

BOOL WINAPI Mine_GetOverlappedResult(HANDLE h, LPOVERLAPPED ov, LPDWORD bytes, BOOL wait) {
    BOOL ok = Real_GetOverlappedResult(h, ov, bytes, wait);
    if (ok && bytes != nullptr) {
        LastErrorGuard guard;
        CompleteRead(ov, *bytes);
    }
    return ok;
}

BOOL WINAPI Mine_GetOverlappedResultEx(HANDLE h, LPOVERLAPPED ov, LPDWORD bytes, DWORD ms, BOOL alertable) {
    BOOL ok = Real_GetOverlappedResultEx(h, ov, bytes, ms, alertable);
    if (ok && bytes != nullptr) {
        LastErrorGuard guard;
        CompleteRead(ov, *bytes);
    }
    return ok;
}

BOOL WINAPI Mine_GetQueuedCompletionStatus(HANDLE port, LPDWORD bytes, PULONG_PTR key, LPOVERLAPPED* ov, DWORD ms) {
    BOOL ok = Real_GetQueuedCompletionStatus(port, bytes, key, ov, ms);
    if (ok && ov != nullptr && *ov != nullptr && bytes != nullptr) {
        LastErrorGuard guard;
        CompleteRead(*ov, *bytes);
    }
    return ok;
}

BOOL WINAPI Mine_GetQueuedCompletionStatusEx(HANDLE port, LPOVERLAPPED_ENTRY entries, ULONG count, PULONG removed,
                                             DWORD ms, BOOL alertable) {
    BOOL ok = Real_GetQueuedCompletionStatusEx(port, entries, count, removed, ms, alertable);
    if (ok && removed != nullptr) {
        LastErrorGuard guard;
        for (ULONG i = 0; i < *removed; ++i) {
            if (entries[i].lpOverlapped != nullptr) CompleteRead(entries[i].lpOverlapped, entries[i].dwNumberOfBytesTransferred);
        }
    }
    return ok;
}

BOOL WINAPI Mine_WaitCommEvent(HANDLE h, LPDWORD mask, LPOVERLAPPED ov) {
    BOOL ok = Real_WaitCommEvent(h, mask, ov);
    Tracked info;
    if (ok && mask != nullptr && Handles().Find(Key(h), &info)) {
        LastErrorGuard guard;
        EmitSignal(info.transport, Key(h), Json().str("api", "WaitCommEvent").unum("mask", *mask).done());
    }
    return ok;
}

// Serial IOCTLs: CTL_CODE(FILE_DEVICE_SERIAL_PORT, n, METHOD_BUFFERED, FILE_ANY_ACCESS).
constexpr DWORD SerialIoctl(DWORD n) { return (0x1Bu << 16) | (n << 2); }
constexpr DWORD kSetBaudRate = SerialIoctl(1), kSetLineControl = SerialIoctl(3), kSetBreakOn = SerialIoctl(4),
                kSetBreakOff = SerialIoctl(5), kSetTimeouts = SerialIoctl(7), kSetDtr = SerialIoctl(9),
                kClrDtr = SerialIoctl(10), kSetRts = SerialIoctl(12), kClrRts = SerialIoctl(13),
                kPurge = SerialIoctl(19), kSetHandflow = SerialIoctl(25);

const char* ParityName(int p) {
    static const char* names[] = {"N", "O", "E", "M", "S"};
    return (p >= 0 && p < 5) ? names[p] : "N";
}

double StopBits(int code) { return code == 1 ? 1.5 : code == 2 ? 2.0 : 1.0; }

void DecodeSerialIoctl(HANDLE h, const Tracked& info, DWORD code, const void* in, DWORD inLen) {
    const auto* bytes = static_cast<const std::uint8_t*>(in);
    Json meta;
    meta.str("api", "DeviceIoControl").unum("ioctl", code);
    if (code == kSetBaudRate && inLen >= 4) {
        meta.unum("baudrate", *reinterpret_cast<const ULONG*>(bytes));
        EmitConfig(info.transport, Key(h), meta.done());
    } else if (code == kSetLineControl && inLen >= 3) {
        meta.real("stopbits", StopBits(bytes[0])).str("parity", ParityName(bytes[1])).unum("bytesize", bytes[2]);
        EmitConfig(info.transport, Key(h), meta.done());
    } else if (code == kSetHandflow && inLen >= 8) {
        const ULONG control = reinterpret_cast<const ULONG*>(bytes)[0];
        const ULONG replace = reinterpret_cast<const ULONG*>(bytes)[1];
        meta.boolean("rtscts", (control & 0x08) != 0 || (replace & 0xC0) == 0x80)
            .boolean("dsrdtr", (control & 0x10) != 0)
            .boolean("xonxoff", (replace & 0x03) != 0);
        EmitConfig(info.transport, Key(h), meta.done());
    } else if (code == kSetTimeouts && inLen >= 20) {
        const auto* t = reinterpret_cast<const ULONG*>(bytes);
        meta.unum("read_interval_ms", t[0]).unum("read_timeout_ms", t[2]).unum("write_timeout_ms", t[4]);
        EmitConfig(info.transport, Key(h), meta.done());
    } else if (code == kSetDtr || code == kClrDtr || code == kSetRts || code == kClrRts || code == kSetBreakOn ||
               code == kSetBreakOff || code == kPurge) {
        const char* name = code == kSetDtr ? "SETDTR" : code == kClrDtr ? "CLRDTR" : code == kSetRts ? "SETRTS"
                         : code == kClrRts ? "CLRRTS" : code == kSetBreakOn ? "SETBREAK" : code == kSetBreakOff ? "CLRBREAK"
                                                                                                                 : "PURGE";
        meta.str("function", name);
        EmitSignal(info.transport, Key(h), meta.done());
    }
}

BOOL WINAPI Mine_DeviceIoControl(HANDLE h, DWORD code, LPVOID in, DWORD inLen, LPVOID out, DWORD outLen,
                                 LPDWORD returned, LPOVERLAPPED ov) {
    BOOL ok = Real_DeviceIoControl(h, code, in, inLen, out, outLen, returned, ov);
    Tracked info;
    if (ok && !Suppress::Active() && (code >> 16) == 0x1B && Handles().Find(Key(h), &info) &&
        info.transport == Transport::Serial) {
        LastErrorGuard guard;
        DecodeSerialIoctl(h, info, code, in, inLen);
    }
    return ok;
}

BOOL WINAPI Mine_SetCommState(HANDLE h, LPDCB dcb) {
    BOOL ok;
    {
        Suppress quiet;
        ok = Real_SetCommState(h, dcb);
    }
    Tracked info;
    if (ok && dcb != nullptr && Handles().Find(Key(h), &info)) {
        LastErrorGuard guard;
        Json meta;
        meta.str("api", "SetCommState")
            .unum("baudrate", dcb->BaudRate)
            .unum("bytesize", dcb->ByteSize)
            .str("parity", ParityName(dcb->Parity))
            .real("stopbits", StopBits(dcb->StopBits))
            .boolean("rtscts", dcb->fOutxCtsFlow != 0 || dcb->fRtsControl == RTS_CONTROL_HANDSHAKE)
            .boolean("dsrdtr", dcb->fOutxDsrFlow != 0 || dcb->fDtrControl == DTR_CONTROL_HANDSHAKE)
            .boolean("xonxoff", dcb->fOutX != 0 || dcb->fInX != 0);
        EmitConfig(info.transport, Key(h), meta.done());
    }
    return ok;
}

BOOL WINAPI Mine_SetCommTimeouts(HANDLE h, LPCOMMTIMEOUTS t) {
    BOOL ok;
    {
        Suppress quiet;
        ok = Real_SetCommTimeouts(h, t);
    }
    Tracked info;
    if (ok && t != nullptr && Handles().Find(Key(h), &info)) {
        LastErrorGuard guard;
        EmitConfig(info.transport, Key(h),
                   Json().str("api", "SetCommTimeouts").unum("read_interval_ms", t->ReadIntervalTimeout)
                       .unum("read_timeout_ms", t->ReadTotalTimeoutConstant)
                       .unum("write_timeout_ms", t->WriteTotalTimeoutConstant).done());
    }
    return ok;
}

BOOL WINAPI Mine_EscapeCommFunction(HANDLE h, DWORD fn) {
    BOOL ok;
    {
        Suppress quiet;
        ok = Real_EscapeCommFunction(h, fn);
    }
    Tracked info;
    if (ok && Handles().Find(Key(h), &info)) {
        LastErrorGuard guard;
        static const char* names[] = {"?", "SETXOFF", "SETXON", "SETRTS", "CLRRTS", "SETDTR", "CLRDTR", "RESETDEV", "SETBREAK", "CLRBREAK"};
        EmitSignal(info.transport, Key(h), Json().str("api", "EscapeCommFunction").str("function", fn < 10 ? names[fn] : "?").done());
    }
    return ok;
}

thread_local int g_load_depth = 0;

// Late-loaded instrument libraries are hooked when the outermost LoadLibrary* call
// returns. The variants do not all route through LoadLibraryExW, so each is hooked;
// the depth counter keeps nested calls (one variant calling another, or loads made
// by a DLL's initialization) from patching an image the loader is still processing.
template <typename F>
HMODULE Loaded(F&& load, DWORD flags) {
    ++g_load_depth;
    HMODULE m = load();
    --g_load_depth;
    if (g_load_depth == 0 && m != nullptr &&
        (flags & (LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_DATAFILE_EXCLUSIVE | LOAD_LIBRARY_AS_IMAGE_RESOURCE)) == 0) {
        LastErrorGuard guard;
        AttachLateModules();
    }
    return m;
}

HMODULE WINAPI Mine_LoadLibraryExW(LPCWSTR name, HANDLE file, DWORD flags) {
    return Loaded([&] { return Real_LoadLibraryExW(name, file, flags); }, flags);
}

HMODULE WINAPI Mine_LoadLibraryExA(LPCSTR name, HANDLE file, DWORD flags) {
    return Loaded([&] { return Real_LoadLibraryExA(name, file, flags); }, flags);
}

HMODULE WINAPI Mine_LoadLibraryW(LPCWSTR name) {
    return Loaded([&] { return Real_LoadLibraryW(name); }, 0);
}

HMODULE WINAPI Mine_LoadLibraryA(LPCSTR name) {
    return Loaded([&] { return Real_LoadLibraryA(name); }, 0);
}

// Safety net: an application resolving a function from a library we have not hooked
// yet (loaded through a path we do not see) gets the library hooked first.
FARPROC WINAPI Mine_GetProcAddress(HMODULE module, LPCSTR name) {
    if (g_load_depth == 0) {
        LastErrorGuard guard;
        AttachLateModules();
    }
    return Real_GetProcAddress(module, name);
}

}  // namespace

bool TakeUsbDevicePath(HANDLE device, std::string* path) {
    std::lock_guard lock(g_usb_mutex);
    auto it = g_usb_paths.find(Key(device));
    if (it == g_usb_paths.end()) return false;
    *path = it->second;
    return true;
}

std::vector<Hook>& FileHooks() {
    static std::vector<Hook> hooks = {
        {reinterpret_cast<void**>(&Real_CreateFileW), reinterpret_cast<void*>(Mine_CreateFileW), "CreateFileW", false},
        {reinterpret_cast<void**>(&Real_CreateFileA), reinterpret_cast<void*>(Mine_CreateFileA), "CreateFileA", false},
        {reinterpret_cast<void**>(&Real_CloseHandle), reinterpret_cast<void*>(Mine_CloseHandle), "CloseHandle", false},
        {reinterpret_cast<void**>(&Real_ReadFile), reinterpret_cast<void*>(Mine_ReadFile), "ReadFile", false},
        {reinterpret_cast<void**>(&Real_WriteFile), reinterpret_cast<void*>(Mine_WriteFile), "WriteFile", false},
        {reinterpret_cast<void**>(&Real_ReadFileEx), reinterpret_cast<void*>(Mine_ReadFileEx), "ReadFileEx", false},
        {reinterpret_cast<void**>(&Real_WriteFileEx), reinterpret_cast<void*>(Mine_WriteFileEx), "WriteFileEx", false},
        {reinterpret_cast<void**>(&Real_GetOverlappedResult), reinterpret_cast<void*>(Mine_GetOverlappedResult), "GetOverlappedResult", false},
        {reinterpret_cast<void**>(&Real_GetOverlappedResultEx), reinterpret_cast<void*>(Mine_GetOverlappedResultEx), "GetOverlappedResultEx", false},
        {reinterpret_cast<void**>(&Real_GetQueuedCompletionStatus), reinterpret_cast<void*>(Mine_GetQueuedCompletionStatus), "GetQueuedCompletionStatus", false},
        {reinterpret_cast<void**>(&Real_GetQueuedCompletionStatusEx), reinterpret_cast<void*>(Mine_GetQueuedCompletionStatusEx), "GetQueuedCompletionStatusEx", false},
        {reinterpret_cast<void**>(&Real_WaitCommEvent), reinterpret_cast<void*>(Mine_WaitCommEvent), "WaitCommEvent", false},
        {reinterpret_cast<void**>(&Real_DeviceIoControl), reinterpret_cast<void*>(Mine_DeviceIoControl), "DeviceIoControl", false},
        {reinterpret_cast<void**>(&Real_SetCommState), reinterpret_cast<void*>(Mine_SetCommState), "SetCommState", false},
        {reinterpret_cast<void**>(&Real_SetCommTimeouts), reinterpret_cast<void*>(Mine_SetCommTimeouts), "SetCommTimeouts", false},
        {reinterpret_cast<void**>(&Real_EscapeCommFunction), reinterpret_cast<void*>(Mine_EscapeCommFunction), "EscapeCommFunction", false},
        {reinterpret_cast<void**>(&Real_LoadLibraryExW), reinterpret_cast<void*>(Mine_LoadLibraryExW), "LoadLibraryExW", false},
        {reinterpret_cast<void**>(&Real_LoadLibraryExA), reinterpret_cast<void*>(Mine_LoadLibraryExA), "LoadLibraryExA", false},
        {reinterpret_cast<void**>(&Real_LoadLibraryW), reinterpret_cast<void*>(Mine_LoadLibraryW), "LoadLibraryW", false},
        {reinterpret_cast<void**>(&Real_LoadLibraryA), reinterpret_cast<void*>(Mine_LoadLibraryA), "LoadLibraryA", false},
        {reinterpret_cast<void**>(&Real_GetProcAddress), reinterpret_cast<void*>(Mine_GetProcAddress), "GetProcAddress", false},
    };
    return hooks;
}

}  // namespace calder
