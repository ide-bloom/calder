// calder_launch32.exe / calder_launch64.exe
//
// Usage: calder_launch<NN>.exe <capture-dll> <application.exe> [arguments...]
//
// Starts the application suspended with the capture DLL injected through
// DetourCreateProcessWithDllExW, prints "PID <n>" on stdout so the recorder can attach
// its UI Automation helper, resumes the application, and exits with its exit code.
// The DLL is never injected into any other process.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <detours/detours.h>

#include <cstdio>
#include <string>

namespace {

// Quotes one argument following the CommandLineToArgvW rules: backslashes are literal
// unless they precede a quote, in which case they are doubled.
std::wstring Quote(const std::wstring& arg) {
    if (!arg.empty() && arg.find_first_of(L" \t\"") == std::wstring::npos) return arg;
    std::wstring out = L"\"";
    std::size_t backslashes = 0;
    for (wchar_t c : arg) {
        if (c == L'\\') {
            ++backslashes;
            continue;
        }
        out.append(c == L'"' ? backslashes * 2 + 1 : backslashes, L'\\');
        backslashes = 0;
        out += c;
    }
    out.append(backslashes * 2, L'\\');
    out += L'"';
    return out;
}

std::string AnsiPath(const std::wstring& path) {
    // Detours takes the DLL path as an ANSI string; fall back to the 8.3 form when the
    // path is not representable in the active code page.
    BOOL lossy = FALSE;
    int n = WideCharToMultiByte(CP_ACP, WC_NO_BEST_FIT_CHARS, path.c_str(), -1, nullptr, 0, nullptr, &lossy);
    std::wstring use = path;
    if (lossy) {
        wchar_t shortPath[MAX_PATH];
        if (GetShortPathNameW(path.c_str(), shortPath, MAX_PATH) > 0) use = shortPath;
        n = WideCharToMultiByte(CP_ACP, 0, use.c_str(), -1, nullptr, 0, nullptr, nullptr);
    }
    std::string out(static_cast<std::size_t>(n > 0 ? n - 1 : 0), '\0');
    WideCharToMultiByte(CP_ACP, 0, use.c_str(), -1, out.data(), n, nullptr, nullptr);
    return out;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc < 3) {
        std::fwprintf(stderr, L"usage: %ls <capture-dll> <application.exe> [arguments...]\n", argv[0]);
        return 2;
    }
    wchar_t dllFull[MAX_PATH];
    if (GetFullPathNameW(argv[1], MAX_PATH, dllFull, nullptr) == 0 ||
        GetFileAttributesW(dllFull) == INVALID_FILE_ATTRIBUTES) {
        std::fwprintf(stderr, L"capture DLL not found: %ls\n", argv[1]);
        return 2;
    }
    std::wstring cmdline = Quote(argv[2]);
    for (int i = 3; i < argc; ++i) {
        cmdline += L' ';
        cmdline += Quote(argv[i]);
    }
    const std::string dll = AnsiPath(dllFull);
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    if (!DetourCreateProcessWithDllExW(argv[2], cmdline.data(), nullptr, nullptr, FALSE,
                                       CREATE_DEFAULT_ERROR_MODE | CREATE_SUSPENDED, nullptr, nullptr, &si, &pi,
                                       dll.c_str(), nullptr)) {
        std::fwprintf(stderr, L"DetourCreateProcessWithDllExW failed: %lu\n", GetLastError());
        return 3;
    }
    std::printf("PID %lu\n", pi.dwProcessId);
    std::fflush(stdout);
    ResumeThread(pi.hThread);
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return static_cast<int>(code);
}
