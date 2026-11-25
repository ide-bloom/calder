from __future__ import annotations

import random

import crcmod
import crcmod.predefined
import pytest

from calder.infer.checksum import search
from calder.runtime.checksums import (
    CRC_BY_NAME,
    CRC_CATALOGUE,
    SIMPLE_SUMS,
    ChecksumSpec,
    crc,
    fletcher16,
    spec_for,
    sum8_twos,
    xor8,
)

CHECK = b"123456789"


@pytest.mark.parametrize("params", CRC_CATALOGUE, ids=lambda p: p.name)
def test_catalogue_check_values(params):
    assert crc(params, CHECK) == params.check


@pytest.mark.parametrize(
    ("name", "predefined"),
    [
        ("CRC-16/MODBUS", "modbus"),
        ("CRC-16/XMODEM", "xmodem"),
        ("CRC-16/KERMIT", "kermit"),
        ("CRC-32/ISO-HDLC", "crc-32"),
        ("CRC-8/SMBUS", "crc-8"),
    ],
)
def test_engine_matches_crcmod(name, predefined):
    reference = crcmod.predefined.mkPredefinedCrcFun(predefined)
    rng = random.Random(7)
    for _ in range(25):
        data = bytes(rng.randrange(256) for _ in range(rng.randint(0, 40)))
        assert crc(CRC_BY_NAME[name], data) == reference(data)


def test_simple_sums():
    assert sum8_twos(bytes.fromhex("110300000003")) == (-(0x11 + 0x03 + 0x03)) & 0xFF
    assert xor8(b"\x01\x02\x04") == 0x07
    assert fletcher16(b"abcde") == 0xC8F0
    assert set(SIMPLE_SUMS) >= {"SUM-8", "XOR-8", "FLETCHER-16", "SUM-16"}


def _frames(spec: ChecksumSpec, n: int, seed: int, prefix: bytes = b"\x7e") -> list[bytes]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        body = prefix + bytes(rng.randrange(256) for _ in range(rng.randint(4, 14)))
        out.append(spec.apply(body + b"\0" * spec.nbytes))
    return out


@pytest.mark.parametrize("params", CRC_CATALOGUE, ids=lambda p: p.name)
def test_search_recovers_every_catalogued_crc(params):
    nbytes = params.width // 8
    spec = spec_for(params.name, start=1, end=-nbytes, position=-nbytes, endian="le" if params.refin else "be")
    found = search(_frames(spec, 6, hash(params.name) & 0xFFFF))
    assert found, "no checksum validated every frame"
    # Some catalogue entries are equivalent on these frames (e.g. an init value equal to
    # the CRC of the start byte); the true one must be among the validated candidates.
    assert any(c.spec.algorithm == params.name and c.spec.start == 1 for c in found)


@pytest.mark.parametrize(
    ("name", "start", "endian"),
    [("CRC-16/IBM-3740", 1, "be"), ("CRC-16/MODBUS", 0, "le")],
    ids=["pump", "modbus"],
)
def test_search_recovers_simulator_crcs_exactly(name, start, endian):
    spec = spec_for(name, start=start, end=-2, position=-2, endian=endian)
    best = search(_frames(spec, 8, 3))[0]
    assert (best.spec.algorithm, best.spec.start, best.spec.endian) == (name, start, endian)


@pytest.mark.parametrize("algorithm", sorted(SIMPLE_SUMS))
def test_search_recovers_sums(algorithm):
    width = SIMPLE_SUMS[algorithm][0] // 8
    spec = spec_for(algorithm, start=0, end=-width, position=-width)
    found = search(_frames(spec, 8, 11, prefix=b""))
    assert any(c.spec.algorithm == algorithm for c in found)


def test_search_ascii_hex_checksum():
    spec = spec_for("SUM-8/TWOS-COMPLEMENT", start=0, end=-2, position=-2, encoding="ascii_hex")
    frames = [spec.apply(f"CMD{n:03d}".encode() + b"00") for n in (1, 27, 300, 42, 7)]
    found = search(frames, ascii_hex=True)
    assert any(c.spec.encoding == "ascii_hex" and c.spec.algorithm == "SUM-8/TWOS-COMPLEMENT" for c in found)


def test_search_rejects_inconsistent_frames():
    frames = [b"\x01\x02\x03\x04", b"\x05\x06\x07\x09", b"\x10\x20\x30\x41", b"\x11\x12\x13\x99"]
    assert all(c.spec.width == 8 for c in search(frames)) or not search(frames)


def test_checksum_before_trailer():
    spec = spec_for("CRC-16/XMODEM", start=1, end=-3, position=-3, endian="be")
    frames = []
    rng = random.Random(5)
    for _ in range(6):
        body = b"\x02" + bytes(rng.randrange(256) for _ in range(6))
        frames.append(spec.apply(body + b"\0\0\x03"))
    assert any(c.spec.algorithm == "CRC-16/XMODEM" and c.spec.position == -3 for c in search(frames))


def test_spec_verify_and_hex_coverage():
    spec = ChecksumSpec(
        "SUM-8/TWOS-COMPLEMENT", 8, start=1, end=-2, position=-2, encoding="ascii_hex", extra={"covered": "hex"}
    )
    line = b":110600000177" + b"00"
    filled = spec.apply(line)
    assert spec.verify(filled)
    assert not spec.verify(filled[:-1] + b"0")
