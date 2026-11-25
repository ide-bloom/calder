// Fake VISA library (built as visa32.dll / visa64.dll) for capture tests.
// One loopback "instrument": queries ending in '?' are answered, everything else is
// accepted silently. Only the functions the capture DLL hooks are provided.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>

using ViStatus = long;
using ViUInt32 = unsigned long;
using ViSession = ViUInt32;
#if defined(_WIN64)
using ViAttrState = unsigned long long;
#else
using ViAttrState = ViUInt32;
#endif

namespace {
std::string g_reply;
volatile unsigned long g_last_call = 0;  // distinct bodies keep exports from being folded

void Handle(const std::string& command) {
    if (command.rfind("*IDN?", 0) == 0) {
        g_reply = "CALDER,FAKE-VISA,0,1.0\n";
    } else if (!command.empty() && command.find('?') != std::string::npos) {
        g_reply = "1.234\n";
    }
}
}  // namespace

extern "C" {

ViStatus __stdcall viOpenDefaultRM(ViSession* rm) {
    *rm = 1;
    return 0;
}

ViStatus __stdcall viOpen(ViSession, const char*, ViUInt32, ViUInt32, ViSession* vi) {
    *vi = 0x1001;
    return 0;
}

ViStatus __stdcall viClose(ViSession vi) {
    g_last_call = vi;
    return 0;
}

ViStatus __stdcall viWrite(ViSession, unsigned char* buf, ViUInt32 cnt, ViUInt32* ret) {
    Handle(std::string(reinterpret_cast<char*>(buf), cnt));
    if (ret) *ret = cnt;
    return 0;
}

ViStatus __stdcall viRead(ViSession, unsigned char* buf, ViUInt32 cnt, ViUInt32* ret) {
    const ViUInt32 n = static_cast<ViUInt32>(g_reply.size() < cnt ? g_reply.size() : cnt);
    std::memcpy(buf, g_reply.data(), n);
    g_reply.erase(0, n);
    if (ret) *ret = n;
    return 0;
}

ViStatus __stdcall viBufWrite(ViSession, unsigned char* buf, ViUInt32 cnt, ViUInt32* ret) {
    g_last_call = 0xB0F;
    Handle(std::string(reinterpret_cast<char*>(buf), cnt));
    if (ret) *ret = cnt;
    return 0;
}

ViStatus __stdcall viBufRead(ViSession, unsigned char* buf, ViUInt32 cnt, ViUInt32* ret) {
    g_last_call = 0xB0E;
    const ViUInt32 n = static_cast<ViUInt32>(g_reply.size() < cnt ? g_reply.size() : cnt);
    std::memcpy(buf, g_reply.data(), n);
    g_reply.erase(0, n);
    if (ret) *ret = n;
    return 0;
}

ViStatus __stdcall viVSPrintf(ViSession, unsigned char* buf, const char* fmt, va_list args) {
    std::vsnprintf(reinterpret_cast<char*>(buf), 4096, fmt, args);
    return 0;
}

ViStatus __stdcall viVPrintf(ViSession, const char* fmt, va_list args) {
    char text[4096];
    std::vsnprintf(text, sizeof(text), fmt, args);
    Handle(text);
    return 0;
}

ViStatus __cdecl viPrintf(ViSession vi, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    const ViStatus st = viVPrintf(vi, fmt, args);
    va_end(args);
    return st;
}

ViStatus __stdcall viVScanf(ViSession, const char* fmt, va_list args) {
    const std::string reply = g_reply;
    g_reply.clear();
    return std::vsscanf(reply.c_str(), fmt, args) > 0 ? 0 : -1;
}

ViStatus __cdecl viScanf(ViSession vi, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    const ViStatus st = viVScanf(vi, fmt, args);
    va_end(args);
    return st;
}

ViStatus __stdcall viVQueryf(ViSession vi, const char* wfmt, const char* rfmt, va_list args) {
    Handle(wfmt);  // the fake supports write formats without conversions
    return viVScanf(vi, rfmt, args);
}

ViStatus __cdecl viQueryf(ViSession vi, const char* wfmt, const char* rfmt, ...) {
    va_list args;
    va_start(args, rfmt);
    const ViStatus st = viVQueryf(vi, wfmt, rfmt, args);
    va_end(args);
    return st;
}

ViStatus __stdcall viSetAttribute(ViSession, ViUInt32 attr, ViAttrState value) {
    g_last_call = attr ^ static_cast<ViUInt32>(value);
    return 0;
}

}  // extern "C"
