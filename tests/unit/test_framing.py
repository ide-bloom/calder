from __future__ import annotations

import random
import struct

import pytest

from calder.infer.framing import (
    detect_terminator,
    fixed_length,
    search_length_field,
    split_by_length,
    split_delimited,
)
from calder.infer.streams import Chunk
from calder.runtime.framing import Framer, FramingKind, FramingSpec, LengthField, modbus_rtu_length, split
from calder.session.models import Direction

TX = Direction.TX


def chunks(datas: list[bytes]) -> list[list[Chunk]]:
    return [[Chunk(0, i, i * 1000, TX, 0, d) for i, d in enumerate(datas)]]


def test_terminator_prefers_crlf_over_lf():
    result = detect_terminator(chunks([b"*IDN?\r\n", b"MEAS:TEMP?\r\n", b"OUTP ON\r\n"]))
    assert result is not None
    assert result.terminator == b"\r\n"


def test_terminator_lf_when_bare():
    result = detect_terminator(chunks([b"A\n", b"BB\n", b"CCC\n"]))
    assert result is not None
    assert result.terminator == b"\n"


def test_split_delimited_handles_split_reads():
    groups = chunks([b"12.", b"5\n3", b"4.0\n"])
    frames = split_delimited(groups, b"\n", TX)
    assert [f.raw for f in frames] == [b"12.5\n", b"34.0\n"]


def _frame(opcode: int, payload: bytes, *, start: bytes, header: str) -> bytes:
    if header == "stx-addr-op-len":
        return start + bytes([1, opcode, len(payload)]) + payload + b"\xaa\xbb"
    if header == "len16le-first":
        body = bytes([opcode]) + payload
        return start + struct.pack("<H", len(body) + 4) + body + b"\xcc\xdd"
    raise ValueError(header)


@pytest.mark.parametrize(
    ("start", "header", "offset", "width", "endian", "adjust"),
    [
        (b"\x02", "stx-addr-op-len", 3, 1, "le", 6),
        # The ranking prefers a one-byte start sequence; the second sync byte becomes header.
        (b"\xaa\x55", "len16le-first", 2, 2, "le", 2),
    ],
)
def test_length_field_discovery(start, header, offset, width, endian, adjust):
    rng = random.Random(1)

    def make() -> bytes:
        size = (
            rng.choice([rng.randint(0, 9), rng.randint(260, 300)]) if header == "len16le-first" else rng.randint(0, 9)
        )
        payload = bytes(rng.randrange(256) for _ in range(size))
        return _frame(rng.randrange(1, 9), payload, start=start, header=header)

    requests = [make() for _ in range(40)]
    replies = [make() for _ in range(40)]
    # Host writes carry whole frames; instrument reads are split at arbitrary points.
    stream = b"".join(replies)
    cuts = sorted(rng.sample(range(1, len(stream)), 25))
    pieces = [stream[a:b] for a, b in zip([0, *cuts], [*cuts, len(stream)], strict=True)]
    rx = [[Chunk(0, i, i * 1000, Direction.RX, 0, d) for i, d in enumerate(pieces)]]
    cand, _alts = search_length_field([chunks(requests), rx])
    assert cand is not None
    assert (cand.start, cand.offset, cand.width, cand.endian, cand.adjust) == (start[:1], offset, width, endian, adjust)
    assert [f.raw for f in split_by_length(rx, Direction.RX, cand)] == replies


def test_length_field_prefers_wide_big_endian_when_high_byte_is_zero():
    frames = []
    for n in range(1, 30):
        pdu = bytes([0x11, 0x03]) + bytes(n % 7)
        frames.append(struct.pack(">HHH", n, 0, len(pdu)) + pdu)
    cand, _ = search_length_field([chunks(frames), []])
    assert cand is not None
    assert (cand.offset, cand.width, cand.endian, cand.adjust) == (4, 2, "be", 6)


def test_fixed_length():
    assert fixed_length([chunks([b"\x01" * 16, b"\x02" * 16]), []]) == 16
    assert fixed_length([chunks([b"\x01" * 16, b"\x02" * 15]), []]) is None


def test_runtime_framer_incremental_and_resync():
    spec = FramingSpec(FramingKind.LENGTH_PREFIXED, start=b"\x02", length_field=LengthField(3, 1, "le", 6))
    framer = Framer(spec, tx=True)
    frame = bytes([2, 1, 0x10, 4, 1, 2, 3, 4, 0xAA, 0xBB])
    assert framer.feed(b"\xff\xff" + frame[:5]) == []
    assert framer.feed(frame[5:] + frame) == [frame, frame]


def test_runtime_modbus_lengths():
    assert modbus_rtu_length(bytes([0x11, 0x03]), request=True) == 8
    assert modbus_rtu_length(bytes([0x11, 0x03, 0x06]), request=False) == 11
    assert modbus_rtu_length(bytes([0x11, 0x83]), request=False) == 5
    frames, rest = split(FramingSpec(FramingKind.MODBUS_RTU), bytes([0x11, 6, 0, 0, 0, 1, 0, 0]) + b"\x11", tx=True)
    assert len(frames) == 1
    assert rest == b"\x11"


def test_length_field_write_round_trip():
    lf = LengthField(1, 2, "be", 3)
    frame = lf.write(bytes(10))
    assert lf.read(frame) == 10
