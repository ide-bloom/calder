// Entry point of calder_capture32.dll / calder_capture64.dll.
//
// The DLL is injected only by Calder's launcher (DetourCreateProcessWithDllExW). On
// attach it reads its configuration from the environment the recorder set up:
//   CALDER_RING        name of the shared-memory ring (required; without it the DLL is inert)
//   CALDER_FILTER      ';'-separated resource patterns (empty = all candidates)
//   CALDER_TRANSPORTS  ','-separated transports to record
#include "common.h"

#include <detours/detours.h>

#include <algorithm>
#include <sstream>

namespace calder {

void ResolveVisaHelpers(HMODULE module);

namespace {

std::wstring Env(const wchar_t* name) {
    wchar_t buf[4096];
    const DWORD n = GetEnvironmentVariableW(name, buf, 4096);
    return (n > 0 && n < 4096) ? std::wstring(buf, n) : std::wstring();
}

void LoadSettings() {
    Config& cfg = Settings();
    const std::string filter = Utf8(Env(L"CALDER_FILTER").c_str());
    std::stringstream patterns(filter);
    std::string item;
    while (std::getline(patterns, item, ';')) {
        if (!item.empty()) cfg.patterns.push_back(item);
    }
    const std::string transports = Utf8(Env(L"CALDER_TRANSPORTS").c_str());
    if (!transports.empty()) {
        std::fill(std::begin(cfg.transports), std::end(cfg.transports), false);
        std::stringstream list(transports);
        while (std::getline(list, item, ',')) {
            for (int t = 1; t <= 6; ++t) {
                if (item == TransportName(static_cast<Transport>(t))) cfg.transports[t] = true;
            }
        }
    }
}

struct LateModule {
    const wchar_t* names[2];
    std::vector<Hook>& (*hooks)();
    Transport transport;
    bool done;
};

LateModule g_modules[] = {
    {{L"ws2_32.dll", nullptr}, WinsockHooks, Transport::Tcp, false},
    {{L"visa64.dll", L"visa32.dll"}, VisaHooks, Transport::Visa, false},
    {{L"ftd2xx64.dll", L"ftd2xx.dll"}, FtdiHooks, Transport::Ftdi, false},
    {{L"winusb.dll", nullptr}, WinUsbHooks, Transport::WinUsb, false},
    {{L"hid.dll", nullptr}, HidHooks, Transport::Hid, false},
};

std::mutex g_late_mutex;
thread_local bool g_in_late = false;

std::string Join(const std::vector<std::string>& items) {
    std::string out = "[";
    for (std::size_t i = 0; i < items.size(); ++i) {
        if (i) out += ',';
        out += '"' + Escape(items[i]) + '"';
    }
    return out + "]";
}

}  // namespace

void AttachLateModules() {
    if (g_in_late || !Ring().IsOpen()) return;
    bool pending = false;
    for (const LateModule& m : g_modules) pending = pending || !m.done;
    if (!pending) return;
    g_in_late = true;
    {
        std::lock_guard lock(g_late_mutex);
        for (LateModule& m : g_modules) {
            if (m.done || !Settings().transports[static_cast<int>(m.transport)]) continue;
            for (const wchar_t* name : m.names) {
                if (name == nullptr) continue;
                HMODULE module = GetModuleHandleW(name);
                if (module == nullptr) continue;
                if (m.transport == Transport::Visa) ResolveVisaHelpers(module);
                ResolveAndAttach(module, m.hooks());
                m.done = true;
                std::vector<std::string> names;
                std::vector<std::string> skipped;
                for (const Hook& h : m.hooks()) (h.attached ? names : skipped).emplace_back(h.name);
                EmitInfo(Json().str("event", "hooked").wstr("module", name).raw("hooks", Join(names))
                             .raw("skipped", Join(skipped)).done());
                break;
            }
        }
    }
    g_in_late = false;
}

}  // namespace calder

using namespace calder;

BOOL WINAPI DllMain(HINSTANCE, DWORD reason, LPVOID) {
    if (DetourIsHelperProcess()) return TRUE;
    if (reason == DLL_PROCESS_ATTACH) {
        DetourRestoreAfterWith();
        DisableThreadLibraryCalls(GetModuleHandleW(nullptr));
        const std::wstring ring = []() {
            wchar_t buf[512];
            const DWORD n = GetEnvironmentVariableW(L"CALDER_RING", buf, 512);
            return (n > 0 && n < 512) ? std::wstring(buf, n) : std::wstring();
        }();
        if (ring.empty() || !Ring().Open(ring.c_str())) return TRUE;  // inert without a recorder
        LoadSettings();
        AttachHooks(FileHooks());
        AttachLateModules();
        Json info;
        info.str("event", "attached")
            .num("bitness", sizeof(void*) * 8)
            .num("pid", static_cast<long long>(GetCurrentProcessId()))
            .raw("hooks", Join(AttachedHookNames()));
        EmitInfo(info.done());
    } else if (reason == DLL_PROCESS_DETACH) {
        if (Ring().IsOpen()) {
            EmitInfo(Json().str("event", "detached").done());
            for (auto* group : {&HidHooks(), &WinUsbHooks(), &FtdiHooks(), &VisaHooks(), &WinsockHooks(), &FileHooks()}) {
                DetachHooks(*group);
            }
            Ring().Close();
        }
    }
    return TRUE;
}
