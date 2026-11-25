// calder_hooktest32.exe / calder_hooktest64.exe: exercises every hook family so the
// capture tests can check the DLL without real instruments.
//
// Usage: calder_hooktest<NN>.exe <pipe-name> <tcp-port>
//   pipe:   synchronous and overlapped I/O on \\.\pipe\<pipe-name> (the serial code path)
//   tcp:    connect/send/recv to 127.0.0.1:<tcp-port>
//   VISA:   the fake visa32/visa64.dll next to this executable (viWrite/viRead/viPrintf/viQueryf)
//   FTDI:   the fake ftd2xx.dll next to this executable
//   WinUSB, HID: calls on invalid handles, which the DLL reports as error events
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>

namespace {

int g_failures = 0;

void Check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "ok" : "FAIL", what);
    if (!ok) ++g_failures;
}

HANDLE OpenPipe(const std::wstring& path, DWORD flags) {
    for (int attempt = 0; attempt < 50; ++attempt) {
        HANDLE h = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, flags, nullptr);
        if (h != INVALID_HANDLE_VALUE) return h;
        if (GetLastError() == ERROR_PIPE_BUSY) WaitNamedPipeW(path.c_str(), 200);
        else Sleep(100);
    }
    return INVALID_HANDLE_VALUE;
}

std::string ReadLine(HANDLE h, OVERLAPPED* ov) {
    std::string line;
    char buf[256];
    for (int i = 0; i < 100 && line.find('\n') == std::string::npos; ++i) {
        DWORD got = 0;
        if (ov != nullptr) {
            ResetEvent(ov->hEvent);
            if (!ReadFile(h, buf, sizeof(buf), nullptr, ov) && GetLastError() != ERROR_IO_PENDING) break;
            if (!GetOverlappedResult(h, ov, &got, TRUE)) break;
        } else if (!ReadFile(h, buf, sizeof(buf), &got, nullptr)) {
            break;
        }
        line.append(buf, got);
    }
    return line;
}

void TestPipe(const std::wstring& name) {
    const std::wstring path = L"\\\\.\\pipe\\" + name;
    HANDLE h = OpenPipe(path, 0);
    Check(h != INVALID_HANDLE_VALUE, "pipe open (synchronous)");
    if (h == INVALID_HANDLE_VALUE) return;
    DWORD n = 0;
    const char cmd[] = "*IDN?\n";
    Check(WriteFile(h, cmd, sizeof(cmd) - 1, &n, nullptr) != FALSE, "pipe WriteFile");
    Check(!ReadLine(h, nullptr).empty(), "pipe ReadFile");
    CloseHandle(h);

    h = OpenPipe(path, FILE_FLAG_OVERLAPPED);
    Check(h != INVALID_HANDLE_VALUE, "pipe open (overlapped)");
    if (h == INVALID_HANDLE_VALUE) return;
    OVERLAPPED ov{};
    ov.hEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    const char cmd2[] = "MEAS:TEMP?\n";
    if (!WriteFile(h, cmd2, sizeof(cmd2) - 1, nullptr, &ov) && GetLastError() == ERROR_IO_PENDING) {
        GetOverlappedResult(h, &ov, &n, TRUE);
    }
    Check(!ReadLine(h, &ov).empty(), "pipe overlapped ReadFile + GetOverlappedResult");
    CloseHandle(ov.hEvent);
    CloseHandle(h);
}

void TestTcp(unsigned short port) {
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    Check(connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == 0, "tcp connect");
    const char cmd[] = "*IDN?\n";
    Check(send(s, cmd, sizeof(cmd) - 1, 0) > 0, "tcp send");
    char buf[256];
    std::string reply;
    for (int i = 0; i < 50 && reply.find('\n') == std::string::npos; ++i) {
        const int got = recv(s, buf, sizeof(buf), 0);
        if (got <= 0) break;
        reply.append(buf, got);
    }
    Check(!reply.empty(), "tcp recv");
    closesocket(s);
    WSACleanup();
}

template <typename T>
T Proc(HMODULE m, const char* name) {
    return reinterpret_cast<T>(GetProcAddress(m, name));
}

void TestVisa() {
#if defined(_WIN64)
    const wchar_t* dll = L"visa64.dll";
#else
    const wchar_t* dll = L"visa32.dll";
#endif
    HMODULE m = LoadLibraryW(dll);
    Check(m != nullptr, "load fake VISA");
    if (m == nullptr) return;
    using Open_t = long(__stdcall*)(unsigned long, const char*, unsigned long, unsigned long, unsigned long*);
    using RM_t = long(__stdcall*)(unsigned long*);
    using RW_t = long(__stdcall*)(unsigned long, unsigned char*, unsigned long, unsigned long*);
    using Printf_t = long(__cdecl*)(unsigned long, const char*, ...);
    using Queryf_t = long(__cdecl*)(unsigned long, const char*, const char*, ...);
    using Close_t = long(__stdcall*)(unsigned long);
#if defined(_WIN64)
    using SetAttr_t = long(__stdcall*)(unsigned long, unsigned long, unsigned long long);
#else
    using SetAttr_t = long(__stdcall*)(unsigned long, unsigned long, unsigned long);
#endif
    unsigned long rm = 0, vi = 0, n = 0;
    Proc<RM_t>(m, "viOpenDefaultRM")(&rm);
    Check(Proc<Open_t>(m, "viOpen")(rm, "TCPIP0::192.168.0.50::INSTR", 0, 0, &vi) == 0, "viOpen");
    Proc<SetAttr_t>(m, "viSetAttribute")(vi, 0x3FFF001A, 2000);
    unsigned char idn[] = "*IDN?\n";
    Proc<RW_t>(m, "viWrite")(vi, idn, 6, &n);
    unsigned char buf[128] = {};
    Proc<RW_t>(m, "viRead")(vi, buf, sizeof(buf), &n);
    Check(n > 0, "viWrite/viRead");
    Proc<Printf_t>(m, "viPrintf")(vi, "SOUR:VOLT %.2f\n", 1.5);
    double value = 0;
    Proc<Queryf_t>(m, "viQueryf")(vi, "MEAS:VOLT?\n", "%lf", &value);
    Check(value > 1.0, "viPrintf/viQueryf");
    Proc<Close_t>(m, "viClose")(vi);
    FreeLibrary(m);
}

void TestFtdi() {
    HMODULE m = LoadLibraryW(L"ftd2xx.dll");
    Check(m != nullptr, "load fake FTDI");
    if (m == nullptr) return;
    using OpenEx_t = unsigned long(WINAPI*)(PVOID, DWORD, PVOID*);
    using RW_t = unsigned long(WINAPI*)(PVOID, LPVOID, DWORD, LPDWORD);
    using Baud_t = unsigned long(WINAPI*)(PVOID, ULONG);
    using Data_t = unsigned long(WINAPI*)(PVOID, UCHAR, UCHAR, UCHAR);
    using Close_t = unsigned long(WINAPI*)(PVOID);
    PVOID h = nullptr;
    char serial[] = "FT1234";
    Check(Proc<OpenEx_t>(m, "FT_OpenEx")(serial, 1, &h) == 0, "FT_OpenEx");
    Proc<Baud_t>(m, "FT_SetBaudRate")(h, 19200);
    Proc<Data_t>(m, "FT_SetDataCharacteristics")(h, 8, 0, 0);
    char out[] = "HELLO";
    DWORD n = 0;
    Proc<RW_t>(m, "FT_Write")(h, out, 5, &n);
    char in[16] = {};
    Proc<RW_t>(m, "FT_Read")(h, in, sizeof(in), &n);
    Check(n == 5, "FT_Write/FT_Read");
    Proc<Close_t>(m, "FT_Close")(h);
    FreeLibrary(m);
}

void TestWinUsbAndHid() {
    HMODULE usb = LoadLibraryW(L"winusb.dll");
    if (usb != nullptr) {
        using Init_t = BOOL(WINAPI*)(HANDLE, PVOID*);
        PVOID iface = nullptr;
        Check(Proc<Init_t>(usb, "WinUsb_Initialize")(INVALID_HANDLE_VALUE, &iface) == FALSE, "WinUsb_Initialize on an invalid handle fails");
    }
    HMODULE hid = LoadLibraryW(L"hid.dll");
    if (hid != nullptr) {
        using Feature_t = BOOLEAN(WINAPI*)(HANDLE, PVOID, ULONG);
        unsigned char report[8] = {0x01, 0x02};
        Check(Proc<Feature_t>(hid, "HidD_SetFeature")(INVALID_HANDLE_VALUE, report, sizeof(report)) == FALSE,
              "HidD_SetFeature on an invalid handle fails");
    }
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc < 3) {
        std::fwprintf(stderr, L"usage: %ls <pipe-name> <tcp-port>\n", argv[0]);
        return 2;
    }
    TestPipe(argv[1]);
    TestTcp(static_cast<unsigned short>(_wtoi(argv[2])));
    TestVisa();
    TestFtdi();
    TestWinUsbAndHid();
    std::printf("%d failure(s)\n", g_failures);
    return g_failures == 0 ? 0 : 1;
}
