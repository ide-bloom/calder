"""Import USBPcap and Wireshark captures (pcapng and classic pcap) as sessions.

Supported link types: Ethernet (1), raw IP (101, 228, 229), BSD loopback (0),
Linux cooked (113) and USBPcap (249). TCP payloads are reassembled per
connection and direction (in sequence order, retransmissions dropped); the
peer that accepted the connection, or the well-known port, is the instrument.
USBPcap bulk transfers become WinUSB events and interrupt transfers HID
events; OUT data comes from submissions and IN data from completions.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from calder.session.builder import SessionBuilder
from calder.session.models import Direction, EventKind, Session, Source, Transport

LINKTYPE_NULL = 0
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101
LINKTYPE_LINUX_SLL = 113
LINKTYPE_IPV4 = 228
LINKTYPE_IPV6 = 229
LINKTYPE_USBPCAP = 249
INSTRUMENT_PORTS = {502, 1394, 4000, 4001, 5024, 5025, 5555, 10001, 23, 111}


class CaptureFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Packet:
    t_ns: int
    linktype: int
    data: bytes


def _pcap(data: bytes) -> Iterator[Packet]:
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian = ">"
    else:
        raise CaptureFormatError("not a pcap file")
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    (linktype,) = struct.unpack_from(endian + "I", data, 20)
    pos = 24
    while pos + 16 <= len(data):
        sec, frac, incl, _orig = struct.unpack_from(endian + "IIII", data, pos)
        pos += 16
        t = sec * 1_000_000_000 + (frac if nano else frac * 1000)
        yield Packet(t, linktype, data[pos : pos + incl])
        pos += incl


def _pcapng(data: bytes) -> Iterator[Packet]:
    pos = 0
    endian = "<"
    interfaces: list[tuple[int, int]] = []  # (linktype, ticks per second)
    while pos + 12 <= len(data):
        btype = struct.unpack_from("<I", data, pos)[0]
        if btype == 0x0A0D0D0A:
            bom = data[pos + 8 : pos + 12]
            endian = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            interfaces = []
        (btype, blen) = struct.unpack_from(endian + "II", data, pos)
        if blen < 12 or pos + blen > len(data):
            raise CaptureFormatError(f"truncated block at offset {pos}")
        body = data[pos + 8 : pos + blen - 4]
        if btype == 0x00000001:
            (linktype,) = struct.unpack_from(endian + "H", body, 0)
            resolution = 1_000_000
            opt = 8
            while opt + 4 <= len(body):
                code, length = struct.unpack_from(endian + "HH", body, opt)
                if code == 0:
                    break
                value = body[opt + 4 : opt + 4 + length]
                if code == 9 and value:
                    raw = value[0]
                    resolution = 2 ** (raw & 0x7F) if raw & 0x80 else 10**raw
                opt += 4 + ((length + 3) & ~3)
            interfaces.append((linktype, resolution))
        elif btype == 0x00000006:
            iface, ts_hi, ts_lo, caplen, _orig = struct.unpack_from(endian + "IIIII", body, 0)
            if iface >= len(interfaces):
                raise CaptureFormatError("packet references an unknown interface")
            linktype, resolution = interfaces[iface]
            ticks = (ts_hi << 32) | ts_lo
            yield Packet(ticks * 1_000_000_000 // resolution, linktype, body[20 : 20 + caplen])
        elif btype == 0x00000003 and interfaces:
            (orig,) = struct.unpack_from(endian + "I", body, 0)
            yield Packet(0, interfaces[0][0], body[4 : 4 + orig])
        pos += blen


def read_packets(path: str | Path) -> list[Packet]:
    data = Path(path).read_bytes()
    if data[:4] == b"\x0a\x0d\x0d\x0a":
        return list(_pcapng(data))
    return list(_pcap(data))


@dataclass
class _Segment:
    t_ns: int
    seq: int
    payload: bytes


@dataclass
class _Conn:
    a: tuple[str, int]
    b: tuple[str, int]
    server: tuple[str, int] | None = None
    segments: dict[tuple[str, int], list[_Segment]] = field(default_factory=dict)


def _ip(packet: bytes) -> tuple[str, str, int, bytes] | None:
    if not packet:
        return None
    version = packet[0] >> 4
    if version == 4:
        ihl = (packet[0] & 0x0F) * 4
        proto = packet[9]
        src = ".".join(str(b) for b in packet[12:16])
        dst = ".".join(str(b) for b in packet[16:20])
        total = struct.unpack_from(">H", packet, 2)[0]
        return src, dst, proto, packet[ihl:total]
    if version == 6 and len(packet) >= 40:
        proto = packet[6]
        src = ":".join(f"{x:x}" for x in struct.unpack_from(">8H", packet, 8))
        dst = ":".join(f"{x:x}" for x in struct.unpack_from(">8H", packet, 24))
        return src, dst, proto, packet[40:]
    return None


def _network_payload(p: Packet) -> bytes | None:
    data = p.data
    if p.linktype == LINKTYPE_ETHERNET:
        if len(data) < 14:
            return None
        ethertype = struct.unpack_from(">H", data, 12)[0]
        off = 14
        while ethertype in (0x8100, 0x88A8) and len(data) >= off + 4:
            ethertype = struct.unpack_from(">H", data, off + 2)[0]
            off += 4
        return data[off:] if ethertype in (0x0800, 0x86DD) else None
    if p.linktype in (LINKTYPE_RAW, LINKTYPE_IPV4, LINKTYPE_IPV6):
        return data
    if p.linktype == LINKTYPE_NULL:
        return data[4:]
    if p.linktype == LINKTYPE_LINUX_SLL:
        return data[16:]
    return None


def _tcp_sessions(packets: list[Packet], builder: SessionBuilder) -> None:
    conns: dict[frozenset[tuple[str, int]], _Conn] = {}
    for p in packets:
        net = _network_payload(p)
        if net is None:
            continue
        parsed = _ip(net)
        if parsed is None or parsed[2] != 6:
            continue
        src, dst, _proto, tcp = parsed
        if len(tcp) < 20:
            continue
        sport, dport, seq = struct.unpack_from(">HHI", tcp, 0)
        offset = (tcp[12] >> 4) * 4
        flags = tcp[13]
        a, b = (src, sport), (dst, dport)
        key = frozenset((a, b))
        conn = conns.setdefault(key, _Conn(a, b))
        if flags & 0x12 == 0x12:  # SYN+ACK comes from the server
            conn.server = a
        payload = tcp[offset:]
        if payload:
            conn.segments.setdefault(a, []).append(_Segment(p.t_ns, seq, payload))
    for conn in conns.values():
        server = conn.server
        if server is None:
            server = conn.b if conn.b[1] in INSTRUMENT_PORTS or conn.b[1] < conn.a[1] else conn.a
        stream = f"tcp:{server[0]}:{server[1]}"
        events: list[tuple[int, Direction, bytes]] = []
        for side, segs in conn.segments.items():
            direction = Direction.RX if side == server else Direction.TX
            seen: set[tuple[int, int]] = set()
            for s in sorted(segs, key=lambda s: (s.seq, s.t_ns)):
                span = (s.seq, len(s.payload))
                if span in seen:
                    continue
                seen.add(span)
                events.append((s.t_ns, direction, s.payload))
        if not events:
            continue
        events.sort(key=lambda e: e[0])
        builder.wire(
            max(events[0][0] - 1, 0),
            Transport.TCP,
            stream,
            Direction.NONE,
            b"",
            EventKind.OPEN,
            meta={"remote": f"{server[0]}:{server[1]}"},
        )
        for t, direction, payload in events:
            builder.wire(t, Transport.TCP, stream, direction, payload)


def _usb_sessions(packets: list[Packet], builder: SessionBuilder) -> None:
    opened: set[str] = set()
    for p in packets:
        if p.linktype != LINKTYPE_USBPCAP or len(p.data) < 27:
            continue
        d = p.data
        header_len, _irp, status, _function, info, bus, device, endpoint, transfer, length = struct.unpack_from(
            "<HQIHBHHBBI", d, 0
        )
        payload = d[header_len : header_len + length]
        if status != 0 or not payload:
            continue
        completion = bool(info & 0x01)
        inbound = bool(endpoint & 0x80)
        if transfer == 2:  # control: setup in submissions, data stage follows
            stage = d[27] if len(d) > 27 else 0
            if stage == 0:  # setup stage carries no device data
                continue
        if inbound != completion:
            continue  # IN data arrives with the completion, OUT data with the submission
        transport = Transport.HID if transfer in (1, 2) else Transport.WINUSB
        stream = f"{transport.value}:usb{bus}.{device}"
        if stream not in opened:
            opened.add(stream)
            builder.wire(
                max(p.t_ns - 1, 0),
                transport,
                stream,
                Direction.NONE,
                b"",
                EventKind.OPEN,
                meta={"path": f"usb{bus}.{device}", "bus": bus, "device": device},
            )
        builder.wire(
            p.t_ns,
            transport,
            stream,
            Direction.RX if inbound else Direction.TX,
            payload,
            meta={"endpoint": endpoint, "transfer": transfer},
        )


def import_capture(path: str | Path) -> Session:
    packets = read_packets(path)
    if not packets:
        raise CaptureFormatError("the capture contains no packets")
    t0 = min((p.t_ns for p in packets if p.t_ns), default=0)
    packets = [Packet(max(p.t_ns - t0, 0), p.linktype, p.data) for p in packets]
    builder = SessionBuilder(Source.PCAPNG, description=f"imported from {Path(path).name}")
    _tcp_sessions(packets, builder)
    _usb_sessions(packets, builder)
    session = builder.build()
    if not session.events:
        raise CaptureFormatError("no TCP payloads or USB transfers found")
    return session
