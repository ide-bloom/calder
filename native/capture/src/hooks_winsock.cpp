// Winsock hooks (ws2_32): TCP connections to addresses that pass the filter.
//
//   connect, WSAConnect  track the socket; identity tcp:<address>:<port>
//   send, WSASend        data events (host to instrument)
//   recv, WSARecv        data events (instrument to host); overlapped receives are
//                        remembered and completed by WSAGetOverlappedResult or the
//                        completion-port hooks in hooks_file.cpp
//   closesocket          close event
// ConnectEx (obtained through WSAIoctl) is not hooked; see docs/hooks.md.
#include <ws2tcpip.h>

#include <cstdio>
#include <cstring>

#include "common.h"

namespace calder {
namespace {

using connect_t = int(WSAAPI*)(SOCKET, const sockaddr*, int);
using WSAConnect_t = int(WSAAPI*)(SOCKET, const sockaddr*, int, LPWSABUF, LPWSABUF, LPQOS, LPQOS);
using send_t = int(WSAAPI*)(SOCKET, const char*, int, int);
using recv_t = int(WSAAPI*)(SOCKET, char*, int, int);
using WSASend_t = int(WSAAPI*)(SOCKET, LPWSABUF, DWORD, LPDWORD, DWORD, LPWSAOVERLAPPED, LPWSAOVERLAPPED_COMPLETION_ROUTINE);
using WSARecv_t = int(WSAAPI*)(SOCKET, LPWSABUF, DWORD, LPDWORD, LPDWORD, LPWSAOVERLAPPED, LPWSAOVERLAPPED_COMPLETION_ROUTINE);
using WSAGetOverlappedResult_t = BOOL(WSAAPI*)(SOCKET, LPWSAOVERLAPPED, LPDWORD, BOOL, LPDWORD);
using closesocket_t = int(WSAAPI*)(SOCKET);

connect_t Real_connect = nullptr;
WSAConnect_t Real_WSAConnect = nullptr;
send_t Real_send = nullptr;
recv_t Real_recv = nullptr;
WSASend_t Real_WSASend = nullptr;
WSARecv_t Real_WSARecv = nullptr;
WSAGetOverlappedResult_t Real_WSAGetOverlappedResult = nullptr;
closesocket_t Real_closesocket = nullptr;

std::uint64_t Key(SOCKET s) { return static_cast<std::uint64_t>(s); }

std::string Address(const sockaddr* addr, int len) {
    char text[96] = {};
    if (addr == nullptr) return {};
    if (addr->sa_family == AF_INET && len >= static_cast<int>(sizeof(sockaddr_in))) {
        const auto* in = reinterpret_cast<const sockaddr_in*>(addr);
        const auto* b = reinterpret_cast<const unsigned char*>(&in->sin_addr);
        std::snprintf(text, sizeof(text), "%u.%u.%u.%u:%u", b[0], b[1], b[2], b[3], ntohs(in->sin_port));
        return text;
    }
    if (addr->sa_family == AF_INET6 && len >= static_cast<int>(sizeof(sockaddr_in6))) {
        const auto* in6 = reinterpret_cast<const sockaddr_in6*>(addr);
        const auto* b = reinterpret_cast<const unsigned char*>(&in6->sin6_addr);
        static const unsigned char kMapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0xFF};
        if (std::memcmp(b, kMapped, sizeof(kMapped)) == 0) {
            // Dual-stack sockets (e.g. .NET) connect to IPv4 peers through ::ffff:a.b.c.d;
            // report the IPv4 address so filters and resource names stay natural.
            std::snprintf(text, sizeof(text), "%u.%u.%u.%u:%u", b[12], b[13], b[14], b[15], ntohs(in6->sin6_port));
            return text;
        }
        const auto* w = reinterpret_cast<const unsigned short*>(&in6->sin6_addr);
        std::snprintf(text, sizeof(text), "%x:%x:%x:%x:%x:%x:%x:%x:%u", ntohs(w[0]), ntohs(w[1]), ntohs(w[2]),
                      ntohs(w[3]), ntohs(w[4]), ntohs(w[5]), ntohs(w[6]), ntohs(w[7]), ntohs(in6->sin6_port));
        return text;
    }
    return {};
}

void TrackConnect(SOCKET s, const sockaddr* addr, int len, int result) {
    if (result != 0 && WSAGetLastError() != WSAEWOULDBLOCK) return;
    const std::string remote = Address(addr, len);
    if (remote.empty()) return;
    const std::string identity = "tcp:" + remote;
    if (!Allowed(Transport::Tcp, identity)) return;
    EmitOpen(Transport::Tcp, Key(s), identity, Json().str("remote", remote).str("identity", identity).done());
}

int WSAAPI Mine_connect(SOCKET s, const sockaddr* addr, int len) {
    int r = Real_connect(s, addr, len);
    const int err = WSAGetLastError();
    TrackConnect(s, addr, len, r);
    WSASetLastError(err);
    return r;
}

int WSAAPI Mine_WSAConnect(SOCKET s, const sockaddr* addr, int len, LPWSABUF caller, LPWSABUF callee, LPQOS sqos, LPQOS gqos) {
    int r = Real_WSAConnect(s, addr, len, caller, callee, sqos, gqos);
    const int err = WSAGetLastError();
    TrackConnect(s, addr, len, r);
    WSASetLastError(err);
    return r;
}

int WSAAPI Mine_send(SOCKET s, const char* buf, int len, int flags) {
    int r = Real_send(s, buf, len, flags);
    if (r > 0 && Handles().Find(Key(s), nullptr)) {
        const int err = WSAGetLastError();
        EmitData(Transport::Tcp, Key(s), Direction::Tx, buf, static_cast<std::size_t>(r));
        WSASetLastError(err);
    }
    return r;
}

int WSAAPI Mine_recv(SOCKET s, char* buf, int len, int flags) {
    int r = Real_recv(s, buf, len, flags);
    if (r > 0 && (flags & MSG_PEEK) == 0 && Handles().Find(Key(s), nullptr)) {
        const int err = WSAGetLastError();
        EmitData(Transport::Tcp, Key(s), Direction::Rx, buf, static_cast<std::size_t>(r));
        WSASetLastError(err);
    }
    return r;
}

int WSAAPI Mine_WSASend(SOCKET s, LPWSABUF bufs, DWORD count, LPDWORD sent, DWORD flags, LPWSAOVERLAPPED ov,
                        LPWSAOVERLAPPED_COMPLETION_ROUTINE routine) {
    int r = Real_WSASend(s, bufs, count, sent, flags, ov, routine);
    const int err = WSAGetLastError();
    if ((r == 0 || err == WSA_IO_PENDING) && Handles().Find(Key(s), nullptr)) {
        for (DWORD i = 0; i < count; ++i) EmitData(Transport::Tcp, Key(s), Direction::Tx, bufs[i].buf, bufs[i].len);
    }
    WSASetLastError(err);
    return r;
}

int WSAAPI Mine_WSARecv(SOCKET s, LPWSABUF bufs, DWORD count, LPDWORD received, LPDWORD flags, LPWSAOVERLAPPED ov,
                        LPWSAOVERLAPPED_COMPLETION_ROUTINE routine) {
    const bool tracked = Handles().Find(Key(s), nullptr);
    if (tracked && ov != nullptr) {
        PendingRead p;
        p.handle = Key(s);
        p.transport = Transport::Tcp;
        p.wsabufs.assign(bufs, bufs + count);
        Pending().Add(ov, p);
    }
    int r = Real_WSARecv(s, bufs, count, received, flags, ov, routine);
    const int err = WSAGetLastError();
    if (tracked) {
        if (r == 0) {
            PendingRead p;
            const bool had = ov != nullptr && Pending().Take(ov, &p);
            DWORD left = received != nullptr ? *received : 0;
            if (had || ov == nullptr) {
                for (DWORD i = 0; i < count && left > 0; ++i) {
                    const DWORD n = left < bufs[i].len ? left : bufs[i].len;
                    EmitData(Transport::Tcp, Key(s), Direction::Rx, bufs[i].buf, n);
                    left -= n;
                }
            }
        } else if (ov != nullptr && err != WSA_IO_PENDING) {
            Pending().Take(ov, nullptr);
        }
    }
    WSASetLastError(err);
    return r;
}

BOOL WSAAPI Mine_WSAGetOverlappedResult(SOCKET s, LPWSAOVERLAPPED ov, LPDWORD bytes, BOOL wait, LPDWORD flags) {
    BOOL ok = Real_WSAGetOverlappedResult(s, ov, bytes, wait, flags);
    if (ok && bytes != nullptr) {
        const int err = WSAGetLastError();
        PendingRead p;
        if (Pending().Take(ov, &p)) {
            DWORD left = *bytes;
            for (const WSABUF& b : p.wsabufs) {
                if (left == 0) break;
                const DWORD n = left < b.len ? left : b.len;
                EmitData(Transport::Tcp, Key(s), Direction::Rx, b.buf, n);
                left -= n;
            }
        }
        WSASetLastError(err);
    }
    return ok;
}

int WSAAPI Mine_closesocket(SOCKET s) {
    Tracked info;
    const bool tracked = Handles().Remove(Key(s), &info);
    int r = Real_closesocket(s);
    if (tracked) {
        const int err = WSAGetLastError();
        EmitClose(Transport::Tcp, Key(s));
        WSASetLastError(err);
    }
    return r;
}

}  // namespace

std::vector<Hook>& WinsockHooks() {
    static std::vector<Hook> hooks = {
        {reinterpret_cast<void**>(&Real_connect), reinterpret_cast<void*>(Mine_connect), "connect", false},
        {reinterpret_cast<void**>(&Real_WSAConnect), reinterpret_cast<void*>(Mine_WSAConnect), "WSAConnect", false},
        {reinterpret_cast<void**>(&Real_send), reinterpret_cast<void*>(Mine_send), "send", false},
        {reinterpret_cast<void**>(&Real_recv), reinterpret_cast<void*>(Mine_recv), "recv", false},
        {reinterpret_cast<void**>(&Real_WSASend), reinterpret_cast<void*>(Mine_WSASend), "WSASend", false},
        {reinterpret_cast<void**>(&Real_WSARecv), reinterpret_cast<void*>(Mine_WSARecv), "WSARecv", false},
        {reinterpret_cast<void**>(&Real_WSAGetOverlappedResult), reinterpret_cast<void*>(Mine_WSAGetOverlappedResult), "WSAGetOverlappedResult", false},
        {reinterpret_cast<void**>(&Real_closesocket), reinterpret_cast<void*>(Mine_closesocket), "closesocket", false},
    };
    return hooks;
}

}  // namespace calder
