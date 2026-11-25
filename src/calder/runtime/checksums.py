"""Checksum algorithms: a catalogued, parametrized CRC engine plus simple sums.

The CRC catalogue follows Greg Cook's "Catalogue of parametrised CRC
algorithms" (CRC RevEng). Every entry carries its published check value (the
CRC of ``b"123456789"``), which the test suite verifies.

This module is copied verbatim into generated packages (stdlib only).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class CrcParams:
    name: str
    width: int
    poly: int
    init: int
    refin: bool
    refout: bool
    xorout: int
    check: int | None = None


def _c(name: str, width: int, poly: int, init: int, ref: bool, xorout: int, check: int) -> CrcParams:
    return CrcParams(name, width, poly, init, ref, ref, xorout, check)


CRC_CATALOGUE: tuple[CrcParams, ...] = (
    _c("CRC-8/AUTOSAR", 8, 0x2F, 0xFF, False, 0xFF, 0xDF),
    _c("CRC-8/BLUETOOTH", 8, 0xA7, 0x00, True, 0x00, 0x26),
    _c("CRC-8/CDMA2000", 8, 0x9B, 0xFF, False, 0x00, 0xDA),
    _c("CRC-8/DARC", 8, 0x39, 0x00, True, 0x00, 0x15),
    _c("CRC-8/DVB-S2", 8, 0xD5, 0x00, False, 0x00, 0xBC),
    _c("CRC-8/GSM-A", 8, 0x1D, 0x00, False, 0x00, 0x37),
    _c("CRC-8/GSM-B", 8, 0x49, 0x00, False, 0xFF, 0x94),
    _c("CRC-8/HITAG", 8, 0x1D, 0xFF, False, 0x00, 0xB4),
    _c("CRC-8/I-432-1", 8, 0x07, 0x00, False, 0x55, 0xA1),
    _c("CRC-8/I-CODE", 8, 0x1D, 0xFD, False, 0x00, 0x7E),
    _c("CRC-8/LTE", 8, 0x9B, 0x00, False, 0x00, 0xEA),
    _c("CRC-8/MAXIM-DOW", 8, 0x31, 0x00, True, 0x00, 0xA1),
    _c("CRC-8/MIFARE-MAD", 8, 0x1D, 0xC7, False, 0x00, 0x99),
    _c("CRC-8/NRSC-5", 8, 0x31, 0xFF, False, 0x00, 0xF7),
    _c("CRC-8/OPENSAFETY", 8, 0x2F, 0x00, False, 0x00, 0x3E),
    _c("CRC-8/ROHC", 8, 0x07, 0xFF, True, 0x00, 0xD0),
    _c("CRC-8/SAE-J1850", 8, 0x1D, 0xFF, False, 0xFF, 0x4B),
    _c("CRC-8/SMBUS", 8, 0x07, 0x00, False, 0x00, 0xF4),
    _c("CRC-8/TECH-3250", 8, 0x1D, 0xFF, True, 0x00, 0x97),
    _c("CRC-8/WCDMA", 8, 0x9B, 0x00, True, 0x00, 0x25),
    _c("CRC-16/ARC", 16, 0x8005, 0x0000, True, 0x0000, 0xBB3D),
    _c("CRC-16/CDMA2000", 16, 0xC867, 0xFFFF, False, 0x0000, 0x4C06),
    _c("CRC-16/CMS", 16, 0x8005, 0xFFFF, False, 0x0000, 0xAEE7),
    _c("CRC-16/DDS-110", 16, 0x8005, 0x800D, False, 0x0000, 0x9ECF),
    _c("CRC-16/DECT-R", 16, 0x0589, 0x0000, False, 0x0001, 0x007E),
    _c("CRC-16/DECT-X", 16, 0x0589, 0x0000, False, 0x0000, 0x007F),
    _c("CRC-16/DNP", 16, 0x3D65, 0x0000, True, 0xFFFF, 0xEA82),
    _c("CRC-16/EN-13757", 16, 0x3D65, 0x0000, False, 0xFFFF, 0xC2B7),
    _c("CRC-16/GENIBUS", 16, 0x1021, 0xFFFF, False, 0xFFFF, 0xD64E),
    _c("CRC-16/GSM", 16, 0x1021, 0x0000, False, 0xFFFF, 0xCE3C),
    _c("CRC-16/IBM-3740", 16, 0x1021, 0xFFFF, False, 0x0000, 0x29B1),
    _c("CRC-16/IBM-SDLC", 16, 0x1021, 0xFFFF, True, 0xFFFF, 0x906E),
    _c("CRC-16/ISO-IEC-14443-3-A", 16, 0x1021, 0xC6C6, True, 0x0000, 0xBF05),
    _c("CRC-16/KERMIT", 16, 0x1021, 0x0000, True, 0x0000, 0x2189),
    _c("CRC-16/LJ1200", 16, 0x6F63, 0x0000, False, 0x0000, 0xBDF4),
    _c("CRC-16/M17", 16, 0x5935, 0xFFFF, False, 0x0000, 0x772B),
    _c("CRC-16/MAXIM-DOW", 16, 0x8005, 0x0000, True, 0xFFFF, 0x44C2),
    _c("CRC-16/MCRF4XX", 16, 0x1021, 0xFFFF, True, 0x0000, 0x6F91),
    _c("CRC-16/MODBUS", 16, 0x8005, 0xFFFF, True, 0x0000, 0x4B37),
    _c("CRC-16/NRSC-5", 16, 0x080B, 0xFFFF, True, 0x0000, 0xA066),
    _c("CRC-16/OPENSAFETY-A", 16, 0x5935, 0x0000, False, 0x0000, 0x5D38),
    _c("CRC-16/OPENSAFETY-B", 16, 0x755B, 0x0000, False, 0x0000, 0x20FE),
    _c("CRC-16/PROFIBUS", 16, 0x1DCF, 0xFFFF, False, 0xFFFF, 0xA819),
    _c("CRC-16/RIELLO", 16, 0x1021, 0xB2AA, True, 0x0000, 0x63D0),
    _c("CRC-16/SPI-FUJITSU", 16, 0x1021, 0x1D0F, False, 0x0000, 0xE5CC),
    _c("CRC-16/T10-DIF", 16, 0x8BB7, 0x0000, False, 0x0000, 0xD0DB),
    _c("CRC-16/TELEDISK", 16, 0xA097, 0x0000, False, 0x0000, 0x0FB3),
    _c("CRC-16/TMS37157", 16, 0x1021, 0x89EC, True, 0x0000, 0x26B1),
    _c("CRC-16/UMTS", 16, 0x8005, 0x0000, False, 0x0000, 0xFEE8),
    _c("CRC-16/USB", 16, 0x8005, 0xFFFF, True, 0xFFFF, 0xB4C8),
    _c("CRC-16/XMODEM", 16, 0x1021, 0x0000, False, 0x0000, 0x31C3),
    _c("CRC-32/AIXM", 32, 0x814141AB, 0x00000000, False, 0x00000000, 0x3010BF7F),
    _c("CRC-32/AUTOSAR", 32, 0xF4ACFB13, 0xFFFFFFFF, True, 0xFFFFFFFF, 0x1697D06A),
    _c("CRC-32/BASE91-D", 32, 0xA833982B, 0xFFFFFFFF, True, 0xFFFFFFFF, 0x87315576),
    _c("CRC-32/BZIP2", 32, 0x04C11DB7, 0xFFFFFFFF, False, 0xFFFFFFFF, 0xFC891918),
    _c("CRC-32/CD-ROM-EDC", 32, 0x8001801B, 0x00000000, True, 0x00000000, 0x6EC2EDC4),
    _c("CRC-32/CKSUM", 32, 0x04C11DB7, 0x00000000, False, 0xFFFFFFFF, 0x765E7680),
    _c("CRC-32/ISCSI", 32, 0x1EDC6F41, 0xFFFFFFFF, True, 0xFFFFFFFF, 0xE3069283),
    _c("CRC-32/ISO-HDLC", 32, 0x04C11DB7, 0xFFFFFFFF, True, 0xFFFFFFFF, 0xCBF43926),
    _c("CRC-32/JAMCRC", 32, 0x04C11DB7, 0xFFFFFFFF, True, 0x00000000, 0x340BC6D9),
    _c("CRC-32/MEF", 32, 0x741B8CD7, 0xFFFFFFFF, True, 0x00000000, 0xD2C22F51),
    _c("CRC-32/MPEG-2", 32, 0x04C11DB7, 0xFFFFFFFF, False, 0x00000000, 0x0376E6E7),
    _c("CRC-32/XFER", 32, 0x000000AF, 0x00000000, False, 0x00000000, 0xBD0BE338),
)

CRC_BY_NAME: dict[str, CrcParams] = {p.name: p for p in CRC_CATALOGUE}


def _reflect(value: int, width: int) -> int:
    result = 0
    for _ in range(width):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


@lru_cache(maxsize=4096)
def _table(width: int, poly: int, refin: bool) -> tuple[int, ...]:
    mask = (1 << width) - 1
    table: list[int] = []
    if refin:
        rpoly = _reflect(poly, width)
        for byte in range(256):
            crc = byte
            for _ in range(8):
                crc = (crc >> 1) ^ rpoly if crc & 1 else crc >> 1
            table.append(crc & mask)
    else:
        top = 1 << (width - 1)
        for byte in range(256):
            crc = byte << (width - 8)
            for _ in range(8):
                crc = ((crc << 1) ^ poly) if crc & top else (crc << 1)
            table.append(crc & mask)
    return tuple(table)


def crc(params: CrcParams, data: bytes) -> int:
    """Table-driven CRC for widths that are multiples of 8."""
    width = params.width
    mask = (1 << width) - 1
    table = _table(width, params.poly, params.refin)
    if params.refin:
        reg = _reflect(params.init, width)
        for byte in data:
            reg = (reg >> 8) ^ table[(reg ^ byte) & 0xFF]
        if not params.refout:
            reg = _reflect(reg, width)
    else:
        reg = params.init
        shift = width - 8
        for byte in data:
            reg = ((reg << 8) & mask) ^ table[((reg >> shift) ^ byte) & 0xFF]
        if params.refout:
            reg = _reflect(reg, width)
    return (reg ^ params.xorout) & mask


def sum8(data: bytes) -> int:
    return sum(data) & 0xFF


def sum8_twos(data: bytes) -> int:
    """Two's complement of the byte sum; this is the Modbus ASCII LRC."""
    return (-sum(data)) & 0xFF


def sum8_ones(data: bytes) -> int:
    return (~sum(data)) & 0xFF


def xor8(data: bytes) -> int:
    value = 0
    for byte in data:
        value ^= byte
    return value


def sum16(data: bytes) -> int:
    return sum(data) & 0xFFFF


def sum16_twos(data: bytes) -> int:
    return (-sum(data)) & 0xFFFF


def fletcher16(data: bytes) -> int:
    s1 = s2 = 0
    for byte in data:
        s1 = (s1 + byte) % 255
        s2 = (s2 + s1) % 255
    return (s2 << 8) | s1


SIMPLE_SUMS: dict[str, tuple[int, Callable[[bytes], int]]] = {
    "SUM-8": (8, sum8),
    "SUM-8/TWOS-COMPLEMENT": (8, sum8_twos),
    "SUM-8/ONES-COMPLEMENT": (8, sum8_ones),
    "XOR-8": (8, xor8),
    "SUM-16": (16, sum16),
    "SUM-16/TWOS-COMPLEMENT": (16, sum16_twos),
    "FLETCHER-16": (16, fletcher16),
}


@dataclass(frozen=True)
class ChecksumSpec:
    """Where a checksum sits in a frame and how it is computed.

    ``start`` is the first covered byte (from the frame start). ``end`` is the
    exclusive end of coverage relative to the frame end (``-2`` stops before
    the last two bytes). ``position`` is the checksum's offset from the frame
    end. With ``encoding == "ascii_hex"`` the checksum is written as
    upper-case hex characters.
    """

    algorithm: str
    width: int
    start: int
    end: int
    position: int
    endian: str = "be"
    encoding: str = "raw"
    poly: int | None = None
    init: int | None = None
    refin: bool | None = None
    refout: bool | None = None
    xorout: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def nbytes(self) -> int:
        return self.width // 8 * (2 if self.encoding == "ascii_hex" else 1)

    def params(self) -> CrcParams | None:
        if self.poly is None:
            return None
        return CrcParams(
            self.algorithm,
            self.width,
            self.poly,
            self.init or 0,
            bool(self.refin),
            bool(self.refout),
            self.xorout or 0,
        )

    def compute(self, data: bytes) -> int:
        params = self.params()
        if params is not None:
            return crc(params, data)
        if self.algorithm not in SIMPLE_SUMS:
            raise ValueError(f"unknown checksum algorithm {self.algorithm}")
        return SIMPLE_SUMS[self.algorithm][1](data)

    def render(self, value: int) -> bytes:
        nbytes = self.width // 8
        if self.encoding == "ascii_hex":
            return f"{value:0{nbytes * 2}X}".encode("ascii")
        return value.to_bytes(nbytes, "little" if self.endian == "le" else "big")

    def covered(self, frame: bytes) -> bytes:
        end = len(frame) + self.end
        data = frame[self.start : end]
        if self.extra.get("covered") == "hex":
            # Modbus ASCII: the LRC covers the bytes the hex text encodes.
            return bytes.fromhex(data.decode("ascii"))
        return data

    def location(self, frame: bytes) -> tuple[int, int]:
        begin = len(frame) + self.position
        return begin, begin + self.nbytes

    def verify(self, frame: bytes) -> bool:
        begin, stop = self.location(frame)
        if begin < self.start or stop > len(frame) or len(frame) + self.end <= self.start:
            return False
        try:
            covered = self.covered(frame)
        except ValueError:
            return False
        return frame[begin:stop] == self.render(self.compute(covered))

    def apply(self, frame: bytes) -> bytes:
        """Fill the checksum bytes of a frame whose checksum slot is a placeholder."""
        begin, stop = self.location(frame)
        value = self.render(self.compute(self.covered(frame)))
        return frame[:begin] + value + frame[stop:]

    def describe(self) -> str:
        cover_end = f"end{self.end}" if self.end else "end"
        enc = ", ASCII hex" if self.encoding == "ascii_hex" else ""
        return (
            f"{self.algorithm} over bytes [{self.start}:{cover_end}], stored at end{self.position} ({self.endian}{enc})"
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChecksumSpec:
        return cls(**data)


def spec_for(
    algorithm: str, *, start: int, end: int, position: int, endian: str = "be", encoding: str = "raw"
) -> ChecksumSpec:
    """Build a spec for a catalogued CRC or a simple sum by name."""
    if algorithm in CRC_BY_NAME:
        p = CRC_BY_NAME[algorithm]
        return ChecksumSpec(
            algorithm=p.name,
            width=p.width,
            start=start,
            end=end,
            position=position,
            endian=endian,
            encoding=encoding,
            poly=p.poly,
            init=p.init,
            refin=p.refin,
            refout=p.refout,
            xorout=p.xorout,
        )
    if algorithm in SIMPLE_SUMS:
        width = SIMPLE_SUMS[algorithm][0]
        return ChecksumSpec(
            algorithm=algorithm,
            width=width,
            start=start,
            end=end,
            position=position,
            endian=endian,
            encoding=encoding,
        )
    raise KeyError(algorithm)
