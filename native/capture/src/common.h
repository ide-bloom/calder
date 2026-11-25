// Shared state and helpers for the capture hooks.
#pragma once

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <winsock2.h>
#include <windows.h>

#include <cstdint>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include "calder_ring.h"

namespace calder {

// ---- JSON metadata --------------------------------------------------------------------------

// Minimal JSON object builder; values are escaped, keys are trusted literals.
class Json {
public:
    Json& str(std::string_view key, std::string_view value);
    Json& wstr(std::string_view key, const wchar_t* value);
    Json& num(std::string_view key, long long value);
    Json& unum(std::string_view key, unsigned long long value);
    Json& real(std::string_view key, double value);
    Json& boolean(std::string_view key, bool value);
    Json& raw(std::string_view key, std::string_view json);
    std::string done() const;

private:
    void key(std::string_view k);
    std::string body_;
};

std::string Utf8(const wchar_t* text);
std::string Escape(std::string_view text);

// ---- ring writer --------------------------------------------------------------------------

class RingWriter {
public:
    bool Open(const wchar_t* name);
    void Close();
    bool IsOpen() const { return header_ != nullptr; }
    void Emit(Kind kind, Transport transport, std::uint64_t handle, Direction direction, std::int32_t status,
              const std::string& meta, const void* data, std::size_t data_len);

private:
    HANDLE mapping_ = nullptr;
    RingHeader* header_ = nullptr;
    std::uint8_t* data_ = nullptr;
    SRWLOCK lock_ = SRWLOCK_INIT;
};

RingWriter& Ring();

// ---- configuration ------------------------------------------------------------------------

struct Config {
    std::vector<std::string> patterns;  // lower-case wildcard patterns
    bool transports[7] = {false, true, true, true, true, true, true};
};

Config& Settings();
bool WildcardMatch(std::string_view pattern, std::string_view text);
bool Allowed(Transport transport, const std::string& identity);
const char* TransportName(Transport t);

// ---- tracked handles ----------------------------------------------------------------------

struct Tracked {
    Transport transport = Transport::None;
    std::string identity;
};

class HandleTable {
public:
    void Add(std::uint64_t handle, Tracked info);
    bool Find(std::uint64_t handle, Tracked* out) const;
    bool Remove(std::uint64_t handle, Tracked* out);

private:
    mutable std::shared_mutex mutex_;
    std::unordered_map<std::uint64_t, Tracked> map_;
};

HandleTable& Handles();

// Pending overlapped reads, keyed by OVERLAPPED pointer, so completion reports
// (GetOverlappedResult, completion ports, APC routines) can log the data.
struct PendingRead {
    std::uint64_t handle = 0;
    Transport transport = Transport::None;
    void* buffer = nullptr;
    std::uint32_t capacity = 0;
    std::vector<WSABUF> wsabufs;  // for WSARecv
    LPOVERLAPPED_COMPLETION_ROUTINE user_routine = nullptr;
};

class PendingTable {
public:
    void Add(const void* overlapped, PendingRead read);
    bool Take(const void* overlapped, PendingRead* out);

private:
    std::mutex mutex_;
    std::unordered_map<const void*, PendingRead> map_;
};

PendingTable& Pending();

// Preserves GetLastError/WSAGetLastError across logging so hooks are transparent.
class LastErrorGuard {
public:
    LastErrorGuard() : error_(GetLastError()) {}
    ~LastErrorGuard() { SetLastError(error_); }
    LastErrorGuard(const LastErrorGuard&) = delete;
    LastErrorGuard& operator=(const LastErrorGuard&) = delete;

private:
    DWORD error_;
};

// Thread-local suppression while a higher-level hooked API runs (SetCommState
// calls DeviceIoControl; formatted VISA I/O may call viWrite).
struct Suppress {
    Suppress();
    ~Suppress();
    Suppress(const Suppress&) = delete;
    Suppress& operator=(const Suppress&) = delete;
    static bool Active();
};

void EmitData(Transport t, std::uint64_t handle, Direction d, const void* data, std::size_t len,
              const std::string& meta = {});
void EmitOpen(Transport t, std::uint64_t handle, const std::string& identity, const std::string& meta);
void EmitClose(Transport t, std::uint64_t handle);
void EmitConfig(Transport t, std::uint64_t handle, const std::string& meta);
void EmitSignal(Transport t, std::uint64_t handle, const std::string& meta);
void EmitError(Transport t, std::uint64_t handle, std::int32_t status, const std::string& meta);
void EmitInfo(const std::string& meta);

// ---- hook registration --------------------------------------------------------------------

struct Hook {
    void** real;
    void* detour;
    const char* name;
    bool attached;
};

bool AttachHooks(std::vector<Hook>& hooks);
void DetachHooks(std::vector<Hook>& hooks);
bool ResolveAndAttach(HMODULE module, std::vector<Hook>& hooks);

// Per-module hook groups (defined in the hooks_*.cpp files).
std::vector<Hook>& FileHooks();     // kernel32/kernelbase: files, serial, completion ports, loader
std::vector<Hook>& WinsockHooks();  // ws2_32
std::vector<Hook>& VisaHooks();     // visa32/visa64
std::vector<Hook>& FtdiHooks();     // ftd2xx
std::vector<Hook>& WinUsbHooks();   // winusb
std::vector<Hook>& HidHooks();      // hid

void AttachLateModules();  // hooks DLLs that are loaded (or become loaded) after start
std::vector<std::string> AttachedHookNames();

}  // namespace calder
