// Fake FTDI D2XX library (built as ftd2xx.dll) for capture tests: an echo device.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <cstring>
#include <string>

using FT_STATUS = ULONG;
using FT_HANDLE = PVOID;

namespace {
std::string g_buffer;
int g_device = 0;
volatile int g_last_call = 0;  // distinct bodies keep exports from being folded
}  // namespace

extern "C" {

FT_STATUS WINAPI FT_Open(int index, FT_HANDLE* h) {
    g_last_call = index;
    *h = &g_device;
    return 0;
}

FT_STATUS WINAPI FT_OpenEx(PVOID, DWORD, FT_HANDLE* h) {
    *h = &g_device;
    return 0;
}

FT_STATUS WINAPI FT_Close(FT_HANDLE) {
    g_last_call = -1;
    return 0;
}

FT_STATUS WINAPI FT_Write(FT_HANDLE, LPVOID buf, DWORD n, LPDWORD written) {
    g_buffer.append(static_cast<const char*>(buf), n);
    if (written) *written = n;
    return 0;
}

FT_STATUS WINAPI FT_Read(FT_HANDLE, LPVOID buf, DWORD n, LPDWORD got) {
    const DWORD count = static_cast<DWORD>(g_buffer.size() < n ? g_buffer.size() : n);
    std::memcpy(buf, g_buffer.data(), count);
    g_buffer.erase(0, count);
    if (got) *got = count;
    return 0;
}

FT_STATUS WINAPI FT_GetQueueStatus(FT_HANDLE, LPDWORD n) {
    *n = static_cast<DWORD>(g_buffer.size());
    return 0;
}

FT_STATUS WINAPI FT_SetBaudRate(FT_HANDLE, ULONG baud) {
    g_last_call = static_cast<int>(baud);
    return 0;
}
FT_STATUS WINAPI FT_SetDataCharacteristics(FT_HANDLE, UCHAR bits, UCHAR stop, UCHAR parity) {
    g_last_call = bits * 100 + stop * 10 + parity;
    return 0;
}
FT_STATUS WINAPI FT_SetFlowControl(FT_HANDLE, USHORT flow, UCHAR, UCHAR) {
    g_last_call = 0x10000 + flow;
    return 0;
}
FT_STATUS WINAPI FT_SetTimeouts(FT_HANDLE, ULONG rd, ULONG wr) {
    g_last_call = static_cast<int>(rd + wr * 3);
    return 0;
}

}  // extern "C"
