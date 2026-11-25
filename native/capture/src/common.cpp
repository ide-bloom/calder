// Ring writer, JSON helper, filters and handle tables.
#include "common.h"

#include <detours/detours.h>

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>

namespace calder {

// ---- JSON ---------------------------------------------------------------------------------------

std::string Escape(std::string_view text) {
    std::string out;
    out.reserve(text.size() + 8);
    for (unsigned char c : text) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    return out;
}

std::string Utf8(const wchar_t* text) {
    if (text == nullptr) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, text, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 1) return {};
    std::string out(static_cast<std::size_t>(n - 1), '\0');
    WideCharToMultiByte(CP_UTF8, 0, text, -1, out.data(), n, nullptr, nullptr);
    return out;
}

void Json::key(std::string_view k) {
    if (!body_.empty()) body_ += ',';
    body_ += '"';
    body_ += k;
    body_ += "\":";
}

Json& Json::str(std::string_view k, std::string_view v) {
    key(k);
    body_ += '"';
    body_ += Escape(v);
    body_ += '"';
    return *this;
}

Json& Json::wstr(std::string_view k, const wchar_t* v) { return str(k, Utf8(v)); }

Json& Json::num(std::string_view k, long long v) {
    key(k);
    body_ += std::to_string(v);
    return *this;
}

Json& Json::unum(std::string_view k, unsigned long long v) {
    key(k);
    body_ += std::to_string(v);
    return *this;
}

Json& Json::real(std::string_view k, double v) {
    key(k);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.6g", v);
    body_ += buf;
    return *this;
}

Json& Json::boolean(std::string_view k, bool v) {
    key(k);
    body_ += v ? "true" : "false";
    return *this;
}

Json& Json::raw(std::string_view k, std::string_view json) {
    key(k);
    body_ += json;
    return *this;
}

std::string Json::done() const { return "{" + body_ + "}"; }

// ---- ring ---------------------------------------------------------------------------------------

RingWriter& Ring() {
    static RingWriter ring;
    return ring;
}

bool RingWriter::Open(const wchar_t* name) {
    mapping_ = OpenFileMappingW(FILE_MAP_READ | FILE_MAP_WRITE, FALSE, name);
    if (mapping_ == nullptr) return false;
    void* view = MapViewOfFile(mapping_, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0, 0);
    if (view == nullptr) {
        CloseHandle(mapping_);
        mapping_ = nullptr;
        return false;
    }
    auto* header = static_cast<RingHeader*>(view);
    if (header->magic != kRingMagic || header->version != kRingVersion || header->capacity == 0) {
        UnmapViewOfFile(view);
        CloseHandle(mapping_);
        mapping_ = nullptr;
        return false;
    }
    header->writer_pid = GetCurrentProcessId();
    data_ = static_cast<std::uint8_t*>(view) + kRingHeaderSize;
    header_ = header;
    return true;
}

void RingWriter::Close() {
    AcquireSRWLockExclusive(&lock_);
    if (header_ != nullptr) {
        UnmapViewOfFile(header_);
        header_ = nullptr;
        data_ = nullptr;
    }
    if (mapping_ != nullptr) {
        CloseHandle(mapping_);
        mapping_ = nullptr;
    }
    ReleaseSRWLockExclusive(&lock_);
}

void RingWriter::Emit(Kind kind, Transport transport, std::uint64_t handle, Direction direction, std::int32_t status,
                      const std::string& meta, const void* data, std::size_t data_len) {
    if (header_ == nullptr) return;
    constexpr std::size_t kMaxPayload = 1u << 20;
    data_len = std::min(data_len, kMaxPayload);
    const std::size_t raw = sizeof(RecordHeader) + meta.size() + data_len;
    const std::size_t size = (raw + 7) & ~static_cast<std::size_t>(7);
    RecordHeader rec{};
    rec.size = static_cast<std::uint32_t>(size);
    rec.kind = static_cast<std::uint16_t>(kind);
    rec.transport = static_cast<std::uint16_t>(transport);
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    rec.qpc = static_cast<std::uint64_t>(now.QuadPart);
    rec.pid = GetCurrentProcessId();
    rec.tid = GetCurrentThreadId();
    rec.handle = handle;
    rec.direction = static_cast<std::uint32_t>(direction);
    rec.status = status;
    rec.meta_len = static_cast<std::uint32_t>(meta.size());
    rec.data_len = static_cast<std::uint32_t>(data_len);

    AcquireSRWLockExclusive(&lock_);
    if (header_ != nullptr) {
        const auto capacity = static_cast<std::int64_t>(header_->capacity);
        const std::int64_t write = header_->write_pos;
        const std::int64_t read = InterlockedCompareExchange64(&header_->read_pos, 0, 0);
        if (capacity - (write - read) < static_cast<std::int64_t>(size)) {
            InterlockedIncrement64(&header_->dropped);
        } else {
            std::size_t offset = static_cast<std::size_t>(write % capacity);
            auto put = [&](const void* src, std::size_t len) {
                const auto* bytes = static_cast<const std::uint8_t*>(src);
                while (len > 0) {
                    const std::size_t chunk = std::min(len, static_cast<std::size_t>(capacity) - offset);
                    std::memcpy(data_ + offset, bytes, chunk);
                    offset = (offset + chunk) % static_cast<std::size_t>(capacity);
                    bytes += chunk;
                    len -= chunk;
                }
            };
            put(&rec, sizeof(rec));
            if (!meta.empty()) put(meta.data(), meta.size());
            if (data_len) put(data, data_len);
            static const std::uint8_t zeros[8] = {};
            if (size > raw) put(zeros, size - raw);
            MemoryBarrier();
            InterlockedExchange64(&header_->write_pos, write + static_cast<std::int64_t>(size));
        }
    }
    ReleaseSRWLockExclusive(&lock_);
}

// ---- configuration ------------------------------------------------------------------------------

Config& Settings() {
    static Config config;
    return config;
}

bool WildcardMatch(std::string_view pattern, std::string_view text) {
    std::size_t p = 0, t = 0, star = std::string_view::npos, mark = 0;
    auto lower = [](char c) { return static_cast<char>(std::tolower(static_cast<unsigned char>(c))); };
    while (t < text.size()) {
        if (p < pattern.size() && (pattern[p] == '?' || lower(pattern[p]) == lower(text[t]))) {
            ++p;
            ++t;
        } else if (p < pattern.size() && pattern[p] == '*') {
            star = p++;
            mark = t;
        } else if (star != std::string_view::npos) {
            p = star + 1;
            t = ++mark;
        } else {
            return false;
        }
    }
    while (p < pattern.size() && pattern[p] == '*') ++p;
    return p == pattern.size();
}

const char* TransportName(Transport t) {
    switch (t) {
        case Transport::Serial: return "serial";
        case Transport::Visa: return "visa";
        case Transport::Ftdi: return "ftdi";
        case Transport::WinUsb: return "winusb";
        case Transport::Hid: return "hid";
        case Transport::Tcp: return "tcp";
        default: return "none";
    }
}

bool Allowed(Transport transport, const std::string& identity) {
    const Config& cfg = Settings();
    const auto index = static_cast<std::size_t>(transport);
    if (index >= 7 || !cfg.transports[index]) return false;
    if (cfg.patterns.empty()) return true;
    return std::any_of(cfg.patterns.begin(), cfg.patterns.end(),
                       [&](const std::string& p) { return WildcardMatch(p, identity); });
}

// ---- tables -------------------------------------------------------------------------------------

HandleTable& Handles() {
    static HandleTable table;
    return table;
}

void HandleTable::Add(std::uint64_t handle, Tracked info) {
    std::unique_lock lock(mutex_);
    map_[handle] = std::move(info);
}

bool HandleTable::Find(std::uint64_t handle, Tracked* out) const {
    std::shared_lock lock(mutex_);
    auto it = map_.find(handle);
    if (it == map_.end()) return false;
    if (out) *out = it->second;
    return true;
}

bool HandleTable::Remove(std::uint64_t handle, Tracked* out) {
    std::unique_lock lock(mutex_);
    auto it = map_.find(handle);
    if (it == map_.end()) return false;
    if (out) *out = it->second;
    map_.erase(it);
    return true;
}

PendingTable& Pending() {
    static PendingTable table;
    return table;
}

void PendingTable::Add(const void* overlapped, PendingRead read) {
    std::lock_guard lock(mutex_);
    if (map_.size() > 4096) map_.clear();  // abandoned I/O must not grow without bound
    map_[overlapped] = std::move(read);
}

bool PendingTable::Take(const void* overlapped, PendingRead* out) {
    std::lock_guard lock(mutex_);
    auto it = map_.find(overlapped);
    if (it == map_.end()) return false;
    if (out) *out = std::move(it->second);
    map_.erase(it);
    return true;
}

namespace {
thread_local int g_suppress = 0;
}

Suppress::Suppress() { ++g_suppress; }
Suppress::~Suppress() { --g_suppress; }
bool Suppress::Active() { return g_suppress > 0; }

// ---- emit helpers -------------------------------------------------------------------------------

void EmitData(Transport t, std::uint64_t handle, Direction d, const void* data, std::size_t len,
              const std::string& meta) {
    if (len == 0 || data == nullptr) return;
    Ring().Emit(Kind::Data, t, handle, d, 0, meta, data, len);
}

void EmitOpen(Transport t, std::uint64_t handle, const std::string& identity, const std::string& meta) {
    Handles().Add(handle, Tracked{t, identity});
    Ring().Emit(Kind::Open, t, handle, Direction::None, 0, meta, nullptr, 0);
}

void EmitClose(Transport t, std::uint64_t handle) {
    Ring().Emit(Kind::Close, t, handle, Direction::None, 0, {}, nullptr, 0);
}

void EmitConfig(Transport t, std::uint64_t handle, const std::string& meta) {
    Ring().Emit(Kind::Config, t, handle, Direction::None, 0, meta, nullptr, 0);
}

void EmitSignal(Transport t, std::uint64_t handle, const std::string& meta) {
    Ring().Emit(Kind::Signal, t, handle, Direction::None, 0, meta, nullptr, 0);
}

void EmitError(Transport t, std::uint64_t handle, std::int32_t status, const std::string& meta) {
    Ring().Emit(Kind::Error, t, handle, Direction::None, status, meta, nullptr, 0);
}

void EmitInfo(const std::string& meta) { Ring().Emit(Kind::Info, Transport::None, 0, Direction::None, 0, meta, nullptr, 0); }

// ---- hooks --------------------------------------------------------------------------------------

namespace {
std::mutex g_hook_mutex;
}

bool AttachHooks(std::vector<Hook>& hooks) {
    std::lock_guard lock(g_hook_mutex);
    bool any = false;
    // Exports can alias one implementation (forwarders, identical-code folding); a
    // second detour on the same code would corrupt the first, so attach it once.
    std::vector<void*> targets;
    for (const Hook& h : hooks) {
        if (h.attached && *h.real != nullptr) targets.push_back(DetourCodeFromPointer(*h.real, nullptr));
    }
    if (DetourTransactionBegin() != NO_ERROR) return false;
    DetourUpdateThread(GetCurrentThread());
    for (Hook& h : hooks) {
        if (!h.attached && *h.real != nullptr) {
            void* code = DetourCodeFromPointer(*h.real, nullptr);
            if (std::find(targets.begin(), targets.end(), code) != targets.end()) continue;
            targets.push_back(code);
            if (DetourAttach(h.real, h.detour) == NO_ERROR) {
                h.attached = true;
                any = true;
            }
        }
    }
    if (DetourTransactionCommit() != NO_ERROR) {
        for (Hook& h : hooks) h.attached = false;
        return false;
    }
    return any;
}

void DetachHooks(std::vector<Hook>& hooks) {
    std::lock_guard lock(g_hook_mutex);
    if (DetourTransactionBegin() != NO_ERROR) return;
    DetourUpdateThread(GetCurrentThread());
    for (Hook& h : hooks) {
        if (h.attached) {
            DetourDetach(h.real, h.detour);
            h.attached = false;
        }
    }
    DetourTransactionCommit();
}

bool ResolveAndAttach(HMODULE module, std::vector<Hook>& hooks) {
    if (module == nullptr) return false;
    for (Hook& h : hooks) {
        if (!h.attached && *h.real == nullptr) {
            *h.real = reinterpret_cast<void*>(GetProcAddress(module, h.name));
        }
    }
    return AttachHooks(hooks);
}

std::vector<std::string> AttachedHookNames() {
    std::vector<std::string> names;
    for (auto* group : {&FileHooks(), &WinsockHooks(), &VisaHooks(), &FtdiHooks(), &WinUsbHooks(), &HidHooks()}) {
        for (const Hook& h : *group) {
            if (h.attached) names.emplace_back(h.name);
        }
    }
    return names;
}

}  // namespace calder
