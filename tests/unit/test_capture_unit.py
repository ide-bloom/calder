"""Capture components that run on any platform: ring, filters, PE parsing, pcapng, tap."""

from __future__ import annotations

import struct
import threading
from pathlib import Path

import pytest
from tests.conftest import sessions_for

from calder.capture.filters import CaptureFilter, FilterError
from calder.capture.launcher import LaunchError, pe_bitness
from calder.capture.pcapng import import_capture
from calder.capture.recorder import RecordConverter, identity, normalize_path
from calder.capture.ring import HEADER_SIZE, Record, Ring, RingError, decode_records, encode_record
from calder.capture.tap import TapOptions, tap
from calder.capture.uia import RawUiEvent, parse_line
from calder.infer.pipeline import infer
from calder.session import SessionBuilder, Source


def rec(kind: str = "data", data: bytes = b"abc", **meta: object) -> Record:
    return Record(kind, "serial", 10, 1, 2, 0x44, "tx", 0, dict(meta), data)


def test_ring_round_trip_and_wrap():
    ring = Ring(bytearray(HEADER_SIZE + 256), initialize=True, qpc_frequency=10_000_000)
    for i in range(20):  # wraps the 256-byte region several times
        assert ring.push(rec(data=bytes([i]) * 30, i=i))
        (got,) = ring.drain()
        assert got.data == bytes([i]) * 30
        assert got.meta == {"i": i}
    assert ring.qpc_frequency == 10_000_000


def test_ring_drops_when_full():
    ring = Ring(bytearray(HEADER_SIZE + 128), initialize=True)
    assert ring.push(rec(data=b"x" * 40))
    assert not ring.push(rec(data=b"y" * 60))
    assert ring.dropped == 1


def test_ring_rejects_foreign_memory():
    with pytest.raises(RingError):
        Ring(bytearray(HEADER_SIZE + 64), initialize=False)
    with pytest.raises(RingError):
        list(decode_records(b"\x05\x00\x00\x00" + bytes(44)))


def test_record_encoding_is_padded():
    assert len(encode_record(rec(data=b"12345"))) % 8 == 0


def test_filters():
    f = CaptureFilter.parse("serial:COM5, tcp:*:5025", "serial,tcp")
    assert f.matches("serial:COM5")
    assert f.matches("tcp:10.0.0.2:5025")
    assert not f.matches("tcp:10.0.0.2:80")
    assert not CaptureFilter.parse("", "tcp").matches("serial:COM1")
    assert f.environment()["CALDER_TRANSPORTS"] == "serial,tcp"
    with pytest.raises(FilterError):
        CaptureFilter.parse("bluetooth:*")


def _pe(machine: int) -> bytes:
    dos = bytearray(64)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 64)
    return bytes(dos) + b"PE\0\0" + struct.pack("<H", machine)


def test_pe_bitness(tmp_path: Path):
    (tmp_path / "a.exe").write_bytes(_pe(0x014C))
    (tmp_path / "b.exe").write_bytes(_pe(0x8664))
    (tmp_path / "c.exe").write_bytes(b"#!/bin/sh")
    assert pe_bitness(tmp_path / "a.exe") == 32
    assert pe_bitness(tmp_path / "b.exe") == 64
    with pytest.raises(LaunchError):
        pe_bitness(tmp_path / "c.exe")


def test_record_converter_tracks_handles():
    builder = SessionBuilder(Source.HOOK)
    conv = RecordConverter(builder, qpc0=0, frequency=1_000_000_000)
    conv.add(Record("open", "serial", 5, 1, 1, 7, "none", 0, {"path": "\\\\.\\COM5"}, b""))
    conv.add(Record("data", "serial", 6, 1, 1, 7, "tx", 0, {}, b"*IDN?\n"))
    conv.add(Record("info", "none", 7, 1, 1, 0, "none", 0, {"event": "attached"}, b""))
    conv.add(Record("close", "serial", 8, 1, 1, 7, "none", 0, {}, b""))
    session = builder.build()
    assert [e.stream for e in session.events] == ["serial:COM5"] * 3
    assert conv.info == [{"qpc": 7, "event": "attached"}]
    assert normalize_path("\\\\.\\com12") == "COM12"
    assert identity("tcp", {"remote": "1.2.3.4:5"}, 1) == "tcp:1.2.3.4:5"


def test_uia_line_parsing():
    line = (
        '{"type":"event","kind":"value","qpc":42,'
        '"element":{"automation_id":"SP","label":"Setpoint (°C)"},"value":"37.5"}'
    )
    ev = parse_line(line)
    assert isinstance(ev, RawUiEvent)
    assert (ev.qpc, ev.element.key, ev.value) == (42, "SP", "37.5")
    assert parse_line('{"type":"ready"}') == {"type": "ready"}
    assert parse_line("noise") is None


# ----- pcapng ------------------------------------------------------------------------------


def _block(btype: int, body: bytes) -> bytes:
    body += b"\0" * ((4 - len(body) % 4) % 4)
    n = len(body) + 12
    return struct.pack("<II", btype, n) + body + struct.pack("<I", n)


def _pcapng(linktype: int, packets: list[tuple[int, bytes]]) -> bytes:
    out = _block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    out += _block(1, struct.pack("<HHI", linktype, 0, 65535))
    for t_us, data in packets:
        out += _block(6, struct.pack("<IIIII", 0, t_us >> 32, t_us & 0xFFFFFFFF, len(data), len(data)) + data)
    return out


def _tcp(src: tuple[str, int], dst: tuple[str, int], seq: int, payload: bytes, flags: int = 0x18) -> bytes:
    tcp = struct.pack(">HHIIBBHHH", src[1], dst[1], seq, 0, 5 << 4, flags, 65535, 0, 0) + payload
    ip = struct.pack(
        ">BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(tcp),
        0,
        0,
        64,
        6,
        0,
        bytes(int(x) for x in src[0].split(".")),
        bytes(int(x) for x in dst[0].split(".")),
    )
    return b"\x00" * 12 + b"\x08\x00" + ip + tcp


def test_pcapng_tcp_import_and_inference(tmp_path: Path):
    host, inst = ("10.0.0.1", 50000), ("10.0.0.9", 5025)
    packets = [(0, _tcp(inst, host, 0, b"", flags=0x12))]
    t, seq_h, seq_i = 1000, 1, 1
    s = sessions_for("thermostat")[0]
    for e in s.data_events():
        src, dst = (host, inst) if e.direction.value == "tx" else (inst, host)
        seq = seq_h if src == host else seq_i
        packets.append((t, _tcp(src, dst, seq, e.data)))
        if src == host:
            seq_h += len(e.data)
        else:
            seq_i += len(e.data)
        packets.append((t, _tcp(src, dst, seq, e.data)))  # a retransmission
        t = 1000 + e.t_ns // 1000
    path = tmp_path / "cap.pcapng"
    path.write_bytes(_pcapng(1, packets))
    session = import_capture(path)
    assert session.streams() == ["tcp:10.0.0.9:5025"]
    model = infer([session])
    assert model.family.value == "scpi"
    assert model.transport.kind.value == "tcp"


def _usbpcap(endpoint: int, completion: bool, data: bytes) -> bytes:
    header = struct.pack("<HQIHBHHBBI", 27, 1, 0, 9, 1 if completion else 0, 1, 5, endpoint, 1, len(data))
    return header + data


def test_pcapng_usb_import():
    from tests.simulators.hid_stirrer import Stirrer, report

    sim = Stirrer()
    packets = []
    for i in range(12):
        out = report(1, i, 2)
        packets.append((i * 1000, _usbpcap(0x01, False, out)))
        packets.append((i * 1000 + 5, _usbpcap(0x81, True, sim.respond(out))))
        packets.append((i * 1000 + 6, _usbpcap(0x81, False, b"")))  # IN submission without data
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "usb.pcapng"
        path.write_bytes(_pcapng(249, packets))
        session = import_capture(path)
    data = session.data_events()
    assert {e.stream for e in data} == {"hid:usb1.5"}
    assert [e.direction.value for e in data[:2]] == ["tx", "rx"]


def test_passive_tap_with_fake_ports():
    class Port:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        def read(self, size: int = 1) -> bytes:
            del size
            return self.chunks.pop(0) if self.chunks else b""

        def close(self) -> None:
            pass

    stop = threading.Event()
    a = Port([b"*IDN?\n", b"MEAS:TEMP?\n"])
    b = Port([b"CALDER,TC\n", b"22.5\n"])
    timer = threading.Timer(0.3, stop.set)
    timer.start()
    session = tap(TapOptions("COM5", "COM6", 9600), ports=(a, b), stop=stop)
    data = session.data_events()
    assert {e.direction.value for e in data} == {"tx", "rx"}
    assert b"".join(e.data for e in data if e.direction.value == "tx") == b"*IDN?\nMEAS:TEMP?\n"
    assert session.meta.source.value == "tap"
