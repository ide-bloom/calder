// Hooks for libraries loaded at run time: VISA, FTDI D2XX, WinUSB and HID.
//
// VISA (visa32.dll / visa64.dll):
//   viOpen, viClose          open/close events; identity visa:<resource>
//   viWrite, viBufWrite      data (host to instrument)
//   viRead, viBufRead        data (instrument to host)
//   viPrintf, viVPrintf      the formatted text is rendered with viVSPrintf on a copy of
//                            the arguments and logged as data before the real call
//   viQueryf, viVQueryf      the write half is rendered the same way; read halves of
//                            viScanf/viQueryf are reported as signal events with the format
//                            (their bytes stay inside VISA's formatted-I/O buffer)
//   viSetAttribute           config events, serial attributes decoded
// FTDI D2XX (ftd2xx.dll): FT_Open/FT_OpenEx/FT_Close, FT_Read/FT_Write, FT_SetBaudRate,
//   FT_SetDataCharacteristics, FT_SetFlowControl, FT_SetTimeouts.
// WinUSB (winusb.dll): WinUsb_Initialize/Free, WinUsb_WritePipe/ReadPipe, WinUsb_ControlTransfer.
// HID (hid.dll): HidD_SetFeature/GetFeature/SetOutputReport/GetInputReport; interrupt
//   reports travel through ReadFile/WriteFile on the device handle (hooks_file.cpp).
// Failed device calls are reported as error events so attempts remain visible.
#include <cstdarg>
#include <cstdio>

#include "common.h"

namespace calder {

bool TakeUsbDevicePath(HANDLE device, std::string* path);

namespace {

// ---- VISA ---------------------------------------------------------------------------------------

using ViStatus = long;
using ViUInt32 = unsigned long;
using ViSession = ViUInt32;
#if defined(_WIN64)
using ViAttrState = unsigned long long;
#else
using ViAttrState = ViUInt32;
#endif
#define VI_CALL __stdcall
#define VI_CALLC __cdecl

using viOpen_t = ViStatus(VI_CALL*)(ViSession, const char*, ViUInt32, ViUInt32, ViSession*);
using viClose_t = ViStatus(VI_CALL*)(ViSession);
using viRW_t = ViStatus(VI_CALL*)(ViSession, unsigned char*, ViUInt32, ViUInt32*);
using viVPrintf_t = ViStatus(VI_CALL*)(ViSession, const char*, va_list);
using viVSPrintf_t = ViStatus(VI_CALL*)(ViSession, unsigned char*, const char*, va_list);
using viVScanf_t = ViStatus(VI_CALL*)(ViSession, const char*, va_list);
using viVQueryf_t = ViStatus(VI_CALL*)(ViSession, const char*, const char*, va_list);
using viPrintf_t = ViStatus(VI_CALLC*)(ViSession, const char*, ...);
using viScanf_t = ViStatus(VI_CALLC*)(ViSession, const char*, ...);
using viQueryf_t = ViStatus(VI_CALLC*)(ViSession, const char*, const char*, ...);
using viSetAttribute_t = ViStatus(VI_CALL*)(ViSession, ViUInt32, ViAttrState);

viOpen_t Real_viOpen = nullptr;
viClose_t Real_viClose = nullptr;
viRW_t Real_viWrite = nullptr;
viRW_t Real_viRead = nullptr;
viRW_t Real_viBufWrite = nullptr;
viRW_t Real_viBufRead = nullptr;
viVPrintf_t Real_viVPrintf = nullptr;
viVSPrintf_t Real_viVSPrintf = nullptr;
viVScanf_t Real_viVScanf = nullptr;
viVQueryf_t Real_viVQueryf = nullptr;
viPrintf_t Real_viPrintf = nullptr;
viScanf_t Real_viScanf = nullptr;
viQueryf_t Real_viQueryf = nullptr;
viSetAttribute_t Real_viSetAttribute = nullptr;

thread_local int g_visa_depth = 0;  // nested formatted I/O must be logged once

struct VisaDepth {
    VisaDepth() { ++g_visa_depth; }
    ~VisaDepth() { --g_visa_depth; }
    VisaDepth(const VisaDepth&) = delete;
    VisaDepth& operator=(const VisaDepth&) = delete;
};

bool Tracked_(std::uint64_t key) { return Handles().Find(key, nullptr); }

ViStatus VI_CALL Mine_viOpen(ViSession rm, const char* name, ViUInt32 mode, ViUInt32 timeout, ViSession* vi) {
    ViStatus st = Real_viOpen(rm, name, mode, timeout, vi);
    if (st >= 0 && vi != nullptr && name != nullptr) {
        LastErrorGuard guard;
        const std::string identity = std::string("visa:") + name;
        if (Allowed(Transport::Visa, identity)) {
            EmitOpen(Transport::Visa, *vi, identity, Json().str("resource", name).str("identity", identity).done());
        }
    }
    return st;
}

ViStatus VI_CALL Mine_viClose(ViSession vi) {
    Tracked info;
    const bool tracked = Handles().Remove(vi, &info);
    ViStatus st = Real_viClose(vi);
    if (tracked) EmitClose(Transport::Visa, vi);
    return st;
}

ViStatus VI_CALL Mine_viWrite(ViSession vi, unsigned char* buf, ViUInt32 cnt, ViUInt32* ret) {
    ViStatus st = Real_viWrite(vi, buf, cnt, ret);
    if (st >= 0 && g_visa_depth == 0 && Tracked_(vi)) {
        EmitData(Transport::Visa, vi, Direction::Tx, buf, ret ? *ret : cnt, Json().str("api", "viWrite").done());
    }
    return st;
}

ViStatus VI_CALL Mine_viRead(ViSession vi, unsigned char* buf, ViUInt32 cnt, ViUInt32* ret) {
    ViStatus st = Real_viRead(vi, buf, cnt, ret);
    if (st >= 0 && g_visa_depth == 0 && ret != nullptr && Tracked_(vi)) {
        EmitData(Transport::Visa, vi, Direction::Rx, buf, *ret, Json().str("api", "viRead").done());
    }
    return st;
}

ViStatus VI_CALL Mine_viBufWrite(ViSession vi, unsigned char* buf, ViUInt32 cnt, ViUInt32* ret) {
    ViStatus st = Real_viBufWrite(vi, buf, cnt, ret);
    if (st >= 0 && g_visa_depth == 0 && Tracked_(vi)) {
        EmitData(Transport::Visa, vi, Direction::Tx, buf, ret ? *ret : cnt, Json().str("api", "viBufWrite").done());
    }
    return st;
}

ViStatus VI_CALL Mine_viBufRead(ViSession vi, unsigned char* buf, ViUInt32 cnt, ViUInt32* ret) {
    ViStatus st = Real_viBufRead(vi, buf, cnt, ret);
    if (st >= 0 && g_visa_depth == 0 && ret != nullptr && Tracked_(vi)) {
        EmitData(Transport::Visa, vi, Direction::Rx, buf, *ret, Json().str("api", "viBufRead").done());
    }
    return st;
}

void RenderFormatted(ViSession vi, const char* api, const char* fmt, va_list args) {
    if (Real_viVSPrintf == nullptr || fmt == nullptr || !Tracked_(vi)) return;
    unsigned char text[8192] = {};
    va_list copy;
    va_copy(copy, args);
    const ViStatus st = Real_viVSPrintf(vi, text, fmt, copy);
    va_end(copy);
    if (st < 0) return;
    EmitData(Transport::Visa, vi, Direction::Tx, text, std::strlen(reinterpret_cast<char*>(text)),
             Json().str("api", api).str("format", fmt).done());
}

ViStatus VI_CALL Mine_viVPrintf(ViSession vi, const char* fmt, va_list args) {
    if (g_visa_depth == 0) RenderFormatted(vi, "viVPrintf", fmt, args);
    VisaDepth depth;
    return Real_viVPrintf(vi, fmt, args);
}

ViStatus VI_CALLC Mine_viPrintf(ViSession vi, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    if (g_visa_depth == 0) RenderFormatted(vi, "viPrintf", fmt, args);
    ViStatus st;
    {
        VisaDepth depth;
        st = Real_viVPrintf(vi, fmt, args);
    }
    va_end(args);
    return st;
}

void ReportRead(ViSession vi, const char* api, const char* fmt, ViStatus st) {
    if (!Tracked_(vi)) return;
    EmitSignal(Transport::Visa, vi, Json().str("api", api).str("format", fmt ? fmt : "").num("status", st)
                                        .boolean("bytes_unavailable", true).done());
}

ViStatus VI_CALL Mine_viVScanf(ViSession vi, const char* fmt, va_list args) {
    ViStatus st;
    {
        VisaDepth depth;
        st = Real_viVScanf(vi, fmt, args);
    }
    if (g_visa_depth == 0) ReportRead(vi, "viVScanf", fmt, st);
    return st;
}

ViStatus VI_CALLC Mine_viScanf(ViSession vi, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    ViStatus st;
    {
        VisaDepth depth;
        st = Real_viVScanf(vi, fmt, args);
    }
    va_end(args);
    if (g_visa_depth == 0) ReportRead(vi, "viScanf", fmt, st);
    return st;
}

ViStatus VI_CALL Mine_viVQueryf(ViSession vi, const char* wfmt, const char* rfmt, va_list args) {
    if (g_visa_depth == 0) RenderFormatted(vi, "viVQueryf", wfmt, args);
    ViStatus st;
    {
        VisaDepth depth;
        st = Real_viVQueryf(vi, wfmt, rfmt, args);
    }
    if (g_visa_depth == 0) ReportRead(vi, "viVQueryf", rfmt, st);
    return st;
}

ViStatus VI_CALLC Mine_viQueryf(ViSession vi, const char* wfmt, const char* rfmt, ...) {
    va_list args;
    va_start(args, rfmt);
    if (g_visa_depth == 0) RenderFormatted(vi, "viQueryf", wfmt, args);
    ViStatus st;
    {
        VisaDepth depth;
        st = Real_viVQueryf(vi, wfmt, rfmt, args);
    }
    va_end(args);
    if (g_visa_depth == 0) ReportRead(vi, "viQueryf", rfmt, st);
    return st;
}

ViStatus VI_CALL Mine_viSetAttribute(ViSession vi, ViUInt32 attr, ViAttrState value) {
    ViStatus st = Real_viSetAttribute(vi, attr, value);
    if (st >= 0 && Tracked_(vi)) {
        LastErrorGuard guard;
        Json meta;
        meta.str("api", "viSetAttribute").unum("attribute", attr).unum("value", value);
        switch (attr) {
            case 0x3FFF0021: meta.unum("baudrate", value); break;   // VI_ATTR_ASRL_BAUD
            case 0x3FFF0022: meta.unum("bytesize", value); break;   // VI_ATTR_ASRL_DATA_BITS
            case 0x3FFF0023: {                                       // VI_ATTR_ASRL_PARITY
                static const char* p[] = {"N", "O", "E", "M", "S"};
                meta.str("parity", value < 5 ? p[value] : "N");
                break;
            }
            case 0x3FFF0024: meta.real("stopbits", static_cast<double>(value) / 10.0); break;  // VI_ATTR_ASRL_STOP_BITS
            case 0x3FFF0025:                                                                   // VI_ATTR_ASRL_FLOW_CNTRL
                meta.boolean("xonxoff", (value & 1) != 0).boolean("rtscts", (value & 2) != 0).boolean("dsrdtr", (value & 4) != 0);
                break;
            case 0x3FFF001A: meta.unum("read_timeout_ms", value); break;  // VI_ATTR_TMO_VALUE
            case 0x3FFF0018: meta.unum("termchar", value); break;         // VI_ATTR_TERMCHAR
            case 0x3FFF0038: meta.boolean("termchar_enabled", value != 0); break;
            default: break;
        }
        EmitConfig(Transport::Visa, vi, meta.done());
    }
    return st;
}

// ---- FTDI D2XX ----------------------------------------------------------------------------------

using FT_STATUS = ULONG;
using FT_HANDLE = PVOID;
using FT_Open_t = FT_STATUS(WINAPI*)(int, FT_HANDLE*);
using FT_OpenEx_t = FT_STATUS(WINAPI*)(PVOID, DWORD, FT_HANDLE*);
using FT_Close_t = FT_STATUS(WINAPI*)(FT_HANDLE);
using FT_RW_t = FT_STATUS(WINAPI*)(FT_HANDLE, LPVOID, DWORD, LPDWORD);
using FT_SetBaudRate_t = FT_STATUS(WINAPI*)(FT_HANDLE, ULONG);
using FT_SetDataCharacteristics_t = FT_STATUS(WINAPI*)(FT_HANDLE, UCHAR, UCHAR, UCHAR);
using FT_SetFlowControl_t = FT_STATUS(WINAPI*)(FT_HANDLE, USHORT, UCHAR, UCHAR);
using FT_SetTimeouts_t = FT_STATUS(WINAPI*)(FT_HANDLE, ULONG, ULONG);

FT_Open_t Real_FT_Open = nullptr;
FT_OpenEx_t Real_FT_OpenEx = nullptr;
FT_Close_t Real_FT_Close = nullptr;
FT_RW_t Real_FT_Read = nullptr;
FT_RW_t Real_FT_Write = nullptr;
FT_SetBaudRate_t Real_FT_SetBaudRate = nullptr;
FT_SetDataCharacteristics_t Real_FT_SetDataCharacteristics = nullptr;
FT_SetFlowControl_t Real_FT_SetFlowControl = nullptr;
FT_SetTimeouts_t Real_FT_SetTimeouts = nullptr;

std::uint64_t FtKey(FT_HANDLE h) { return reinterpret_cast<std::uint64_t>(h); }

void FtOpened(FT_HANDLE h, const std::string& id, Json meta) {
    const std::string identity = "ftdi:" + id;
    if (!Allowed(Transport::Ftdi, identity)) return;
    meta.str("identity", identity);
    EmitOpen(Transport::Ftdi, FtKey(h), identity, meta.done());
}

FT_STATUS WINAPI Mine_FT_Open(int index, FT_HANDLE* h) {
    FT_STATUS st = Real_FT_Open(index, h);
    if (st == 0 && h != nullptr) FtOpened(*h, std::to_string(index), Json().num("index", index));
    return st;
}

FT_STATUS WINAPI Mine_FT_OpenEx(PVOID arg, DWORD flags, FT_HANDLE* h) {
    FT_STATUS st = Real_FT_OpenEx(arg, flags, h);
    if (st == 0 && h != nullptr) {
        Json meta;
        std::string id;
        if ((flags & 0x1) != 0 && arg != nullptr) {
            id = static_cast<const char*>(arg);
            meta.str("serial_number", id);
        } else if ((flags & 0x2) != 0 && arg != nullptr) {
            id = static_cast<const char*>(arg);
            meta.str("description", id);
        } else {
            id = "location-" + std::to_string(reinterpret_cast<std::uintptr_t>(arg));
            meta.str("location", id);
        }
        FtOpened(*h, id, std::move(meta));
    }
    return st;
}

FT_STATUS WINAPI Mine_FT_Close(FT_HANDLE h) {
    const bool tracked = Handles().Remove(FtKey(h), nullptr);
    FT_STATUS st = Real_FT_Close(h);
    if (tracked) EmitClose(Transport::Ftdi, FtKey(h));
    return st;
}

FT_STATUS WINAPI Mine_FT_Write(FT_HANDLE h, LPVOID buf, DWORD n, LPDWORD written) {
    FT_STATUS st = Real_FT_Write(h, buf, n, written);
    if (Handles().Find(FtKey(h), nullptr)) {
        if (st == 0) EmitData(Transport::Ftdi, FtKey(h), Direction::Tx, buf, written ? *written : n);
        else EmitError(Transport::Ftdi, FtKey(h), static_cast<std::int32_t>(st), Json().str("api", "FT_Write").done());
    }
    return st;
}

FT_STATUS WINAPI Mine_FT_Read(FT_HANDLE h, LPVOID buf, DWORD n, LPDWORD got) {
    FT_STATUS st = Real_FT_Read(h, buf, n, got);
    if (Handles().Find(FtKey(h), nullptr)) {
        if (st == 0 && got != nullptr) EmitData(Transport::Ftdi, FtKey(h), Direction::Rx, buf, *got);
        else if (st != 0) EmitError(Transport::Ftdi, FtKey(h), static_cast<std::int32_t>(st), Json().str("api", "FT_Read").done());
    }
    return st;
}

FT_STATUS WINAPI Mine_FT_SetBaudRate(FT_HANDLE h, ULONG baud) {
    FT_STATUS st = Real_FT_SetBaudRate(h, baud);
    if (st == 0 && Handles().Find(FtKey(h), nullptr)) {
        EmitConfig(Transport::Ftdi, FtKey(h), Json().str("api", "FT_SetBaudRate").unum("baudrate", baud).done());
    }
    return st;
}

FT_STATUS WINAPI Mine_FT_SetDataCharacteristics(FT_HANDLE h, UCHAR bits, UCHAR stop, UCHAR parity) {
    FT_STATUS st = Real_FT_SetDataCharacteristics(h, bits, stop, parity);
    if (st == 0 && Handles().Find(FtKey(h), nullptr)) {
        static const char* p[] = {"N", "O", "E", "M", "S"};
        EmitConfig(Transport::Ftdi, FtKey(h), Json().str("api", "FT_SetDataCharacteristics").unum("bytesize", bits)
                                                   .real("stopbits", stop == 2 ? 2.0 : 1.0).str("parity", parity < 5 ? p[parity] : "N").done());
    }
    return st;
}

FT_STATUS WINAPI Mine_FT_SetFlowControl(FT_HANDLE h, USHORT flow, UCHAR xon, UCHAR xoff) {
    FT_STATUS st = Real_FT_SetFlowControl(h, flow, xon, xoff);
    if (st == 0 && Handles().Find(FtKey(h), nullptr)) {
        EmitConfig(Transport::Ftdi, FtKey(h), Json().str("api", "FT_SetFlowControl").boolean("rtscts", flow == 0x0100)
                                                   .boolean("dsrdtr", flow == 0x0200).boolean("xonxoff", flow == 0x0400).done());
    }
    return st;
}

FT_STATUS WINAPI Mine_FT_SetTimeouts(FT_HANDLE h, ULONG rd, ULONG wr) {
    FT_STATUS st = Real_FT_SetTimeouts(h, rd, wr);
    if (st == 0 && Handles().Find(FtKey(h), nullptr)) {
        EmitConfig(Transport::Ftdi, FtKey(h), Json().str("api", "FT_SetTimeouts").unum("read_timeout_ms", rd).unum("write_timeout_ms", wr).done());
    }
    return st;
}

// ---- WinUSB -------------------------------------------------------------------------------------

#pragma pack(push, 1)
struct SetupPacket {
    UCHAR RequestType;
    UCHAR Request;
    USHORT Value;
    USHORT Index;
    USHORT Length;
};
#pragma pack(pop)

using WinUsb_Initialize_t = BOOL(WINAPI*)(HANDLE, PVOID*);
using WinUsb_Free_t = BOOL(WINAPI*)(PVOID);
using WinUsb_Pipe_t = BOOL(WINAPI*)(PVOID, UCHAR, PUCHAR, ULONG, PULONG, LPOVERLAPPED);
using WinUsb_ControlTransfer_t = BOOL(WINAPI*)(PVOID, SetupPacket, PUCHAR, ULONG, PULONG, LPOVERLAPPED);

WinUsb_Initialize_t Real_WinUsb_Initialize = nullptr;
WinUsb_Free_t Real_WinUsb_Free = nullptr;
WinUsb_Pipe_t Real_WinUsb_WritePipe = nullptr;
WinUsb_Pipe_t Real_WinUsb_ReadPipe = nullptr;
WinUsb_ControlTransfer_t Real_WinUsb_ControlTransfer = nullptr;

std::uint64_t UsbKey(PVOID h) { return reinterpret_cast<std::uint64_t>(h); }

BOOL WINAPI Mine_WinUsb_Initialize(HANDLE device, PVOID* iface) {
    BOOL ok = Real_WinUsb_Initialize(device, iface);
    LastErrorGuard guard;
    std::string path;
    const bool known = TakeUsbDevicePath(device, &path);
    if (ok && iface != nullptr) {
        const std::string identity = "winusb:" + (known ? path : std::to_string(reinterpret_cast<std::uintptr_t>(device)));
        if (Allowed(Transport::WinUsb, identity)) {
            EmitOpen(Transport::WinUsb, UsbKey(*iface), identity, Json().str("path", known ? path : "").str("identity", identity).done());
        }
    } else if (!ok) {
        EmitError(Transport::WinUsb, reinterpret_cast<std::uint64_t>(device), static_cast<std::int32_t>(GetLastError()),
                  Json().str("api", "WinUsb_Initialize").done());
    }
    return ok;
}

BOOL WINAPI Mine_WinUsb_Free(PVOID iface) {
    const bool tracked = Handles().Remove(UsbKey(iface), nullptr);
    BOOL ok = Real_WinUsb_Free(iface);
    if (tracked) {
        LastErrorGuard guard;
        EmitClose(Transport::WinUsb, UsbKey(iface));
    }
    return ok;
}

void UsbResult(BOOL ok, const char* api, PVOID iface, Direction dir, const void* buf, ULONG len, const std::string& meta) {
    LastErrorGuard guard;
    if (ok) {
        EmitData(Transport::WinUsb, UsbKey(iface), dir, buf, len, meta);
    } else if (GetLastError() != ERROR_IO_PENDING) {
        EmitError(Transport::WinUsb, UsbKey(iface), static_cast<std::int32_t>(GetLastError()), Json().str("api", api).done());
    }
}

BOOL WINAPI Mine_WinUsb_WritePipe(PVOID iface, UCHAR pipe, PUCHAR buf, ULONG len, PULONG done, LPOVERLAPPED ov) {
    BOOL ok = Real_WinUsb_WritePipe(iface, pipe, buf, len, done, ov);
    if (Handles().Find(UsbKey(iface), nullptr) || !ok) {
        UsbResult(ok, "WinUsb_WritePipe", iface, Direction::Tx, buf, done && ok ? *done : len, Json().unum("pipe", pipe).done());
    }
    return ok;
}

BOOL WINAPI Mine_WinUsb_ReadPipe(PVOID iface, UCHAR pipe, PUCHAR buf, ULONG len, PULONG done, LPOVERLAPPED ov) {
    BOOL ok = Real_WinUsb_ReadPipe(iface, pipe, buf, len, done, ov);
    if (Handles().Find(UsbKey(iface), nullptr) || !ok) {
        UsbResult(ok, "WinUsb_ReadPipe", iface, Direction::Rx, buf, done ? *done : 0, Json().unum("pipe", pipe).done());
    }
    return ok;
}

BOOL WINAPI Mine_WinUsb_ControlTransfer(PVOID iface, SetupPacket setup, PUCHAR buf, ULONG len, PULONG done, LPOVERLAPPED ov) {
    BOOL ok = Real_WinUsb_ControlTransfer(iface, setup, buf, len, done, ov);
    if (Handles().Find(UsbKey(iface), nullptr) || !ok) {
        const Direction dir = (setup.RequestType & 0x80) != 0 ? Direction::Rx : Direction::Tx;
        const std::string meta = Json().str("api", "WinUsb_ControlTransfer").unum("bmRequestType", setup.RequestType)
                                     .unum("bRequest", setup.Request).unum("wValue", setup.Value).unum("wIndex", setup.Index).done();
        UsbResult(ok, "WinUsb_ControlTransfer", iface, dir, buf, done && ok ? *done : len, meta);
    }
    return ok;
}

// ---- HID ----------------------------------------------------------------------------------------

using HidReport_t = BOOLEAN(WINAPI*)(HANDLE, PVOID, ULONG);
HidReport_t Real_HidD_SetFeature = nullptr;
HidReport_t Real_HidD_GetFeature = nullptr;
HidReport_t Real_HidD_SetOutputReport = nullptr;
HidReport_t Real_HidD_GetInputReport = nullptr;

BOOLEAN HidCall(HidReport_t real, const char* api, Direction dir, HANDLE h, PVOID buf, ULONG len) {
    BOOLEAN ok;
    {
        Suppress quiet;  // HidD_* use DeviceIoControl internally
        ok = real(h, buf, len);
    }
    LastErrorGuard guard;
    const auto key = reinterpret_cast<std::uint64_t>(h);
    if (ok && Handles().Find(key, nullptr)) {
        EmitData(Transport::Hid, key, dir, buf, len, Json().str("api", api).done());
    } else if (!ok) {
        EmitError(Transport::Hid, key, static_cast<std::int32_t>(GetLastError()), Json().str("api", api).done());
    }
    return ok;
}

BOOLEAN WINAPI Mine_HidD_SetFeature(HANDLE h, PVOID b, ULONG n) { return HidCall(Real_HidD_SetFeature, "HidD_SetFeature", Direction::Tx, h, b, n); }
BOOLEAN WINAPI Mine_HidD_GetFeature(HANDLE h, PVOID b, ULONG n) { return HidCall(Real_HidD_GetFeature, "HidD_GetFeature", Direction::Rx, h, b, n); }
BOOLEAN WINAPI Mine_HidD_SetOutputReport(HANDLE h, PVOID b, ULONG n) { return HidCall(Real_HidD_SetOutputReport, "HidD_SetOutputReport", Direction::Tx, h, b, n); }
BOOLEAN WINAPI Mine_HidD_GetInputReport(HANDLE h, PVOID b, ULONG n) { return HidCall(Real_HidD_GetInputReport, "HidD_GetInputReport", Direction::Rx, h, b, n); }

#define HOOK(fn) {reinterpret_cast<void**>(&Real_##fn), reinterpret_cast<void*>(Mine_##fn), #fn, false}

}  // namespace

std::vector<Hook>& VisaHooks() {
    static std::vector<Hook> hooks = {HOOK(viOpen), HOOK(viClose), HOOK(viWrite), HOOK(viRead), HOOK(viBufWrite),
                                      HOOK(viBufRead), HOOK(viVPrintf), HOOK(viPrintf), HOOK(viVScanf), HOOK(viScanf),
                                      HOOK(viVQueryf), HOOK(viQueryf), HOOK(viSetAttribute)};
    return hooks;
}

void ResolveVisaHelpers(HMODULE module) {
    if (Real_viVSPrintf == nullptr) Real_viVSPrintf = reinterpret_cast<viVSPrintf_t>(GetProcAddress(module, "viVSPrintf"));
}

std::vector<Hook>& FtdiHooks() {
    static std::vector<Hook> hooks = {HOOK(FT_Open), HOOK(FT_OpenEx), HOOK(FT_Close), HOOK(FT_Read), HOOK(FT_Write),
                                      HOOK(FT_SetBaudRate), HOOK(FT_SetDataCharacteristics), HOOK(FT_SetFlowControl),
                                      HOOK(FT_SetTimeouts)};
    return hooks;
}

std::vector<Hook>& WinUsbHooks() {
    static std::vector<Hook> hooks = {HOOK(WinUsb_Initialize), HOOK(WinUsb_Free), HOOK(WinUsb_WritePipe),
                                      HOOK(WinUsb_ReadPipe), HOOK(WinUsb_ControlTransfer)};
    return hooks;
}

std::vector<Hook>& HidHooks() {
    static std::vector<Hook> hooks = {HOOK(HidD_SetFeature), HOOK(HidD_GetFeature), HOOK(HidD_SetOutputReport),
                                      HOOK(HidD_GetInputReport)};
    return hooks;
}

}  // namespace calder
