"""Field value encodings shared by inference and generated drivers.

This module is copied verbatim into generated packages, so it depends on the
standard library only.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

Value = int | float | str | bool


class EncodingKind(StrEnum):
    UINT = "uint"
    INT = "int"
    FLOAT = "float"
    BCD = "bcd"
    BITS = "bits"
    BYTES = "bytes"
    ASCII_INT = "ascii_int"
    ASCII_FLOAT = "ascii_float"
    ASCII_HEX = "ascii_hex"
    ASCII_ENUM = "ascii_enum"
    ASCII_STRING = "ascii_string"


BINARY_KINDS = frozenset(
    {
        EncodingKind.UINT,
        EncodingKind.INT,
        EncodingKind.FLOAT,
        EncodingKind.BCD,
        EncodingKind.BITS,
        EncodingKind.BYTES,
    }
)
NUMERIC_KINDS = frozenset(
    {
        EncodingKind.UINT,
        EncodingKind.INT,
        EncodingKind.FLOAT,
        EncodingKind.BCD,
        EncodingKind.BITS,
        EncodingKind.ASCII_INT,
        EncodingKind.ASCII_FLOAT,
        EncodingKind.ASCII_HEX,
    }
)


class EncodingError(ValueError):
    """A value cannot be represented, or wire data does not match the encoding."""


@dataclass(frozen=True)
class Encoding:
    """How one field's value is represented on the wire.

    ``scale_exp`` applies to integer kinds: physical = raw * 10**scale_exp.
    ``width`` is in bytes for binary kinds and in characters for fixed-width
    ASCII (0 means variable width).
    """

    kind: EncodingKind
    width: int = 0
    endian: str = "le"
    scale_exp: int = 0
    decimals: int | None = None
    min_decimals: int = 0
    plus_sign: bool = False
    zero_pad: bool = False
    exponent: bool = False
    upper: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Encoding:
        values = dict(data)
        values["kind"] = EncodingKind(values["kind"])
        return cls(**values)

    @property
    def is_binary(self) -> bool:
        return self.kind in BINARY_KINDS

    @property
    def is_numeric(self) -> bool:
        return self.kind in NUMERIC_KINDS

    def describe(self) -> str:
        k = self.kind
        if k in (EncodingKind.UINT, EncodingKind.INT, EncodingKind.BITS):
            sign = "u" if k is not EncodingKind.INT else "i"
            text = f"{sign}{self.width * 8} {self.endian}" if self.width > 1 else f"{sign}8"
            if k is EncodingKind.BITS:
                text = f"bit field ({text})"
            if self.scale_exp:
                text += f" x 10^{self.scale_exp}"
            return text
        if k is EncodingKind.FLOAT:
            name = "IEEE 754 single" if self.width == 4 else "IEEE 754 double"
            return f"{name} {self.endian}"
        if k is EncodingKind.BCD:
            text = f"BCD ({self.width} bytes, {self.endian})"
            return text + (f" x 10^{self.scale_exp}" if self.scale_exp else "")
        if k is EncodingKind.ASCII_FLOAT:
            parts = ["ASCII decimal"]
            if self.decimals is not None:
                parts.append(f"{self.decimals} decimals")
            if self.exponent:
                parts.append("exponent")
            if self.width:
                parts.append(f"width {self.width}")
            if self.plus_sign:
                parts.append("explicit sign")
            return ", ".join(parts)
        if k is EncodingKind.ASCII_INT:
            return "ASCII integer" + (f", width {self.width}" if self.width else "")
        if k is EncodingKind.ASCII_HEX:
            return "ASCII hexadecimal" + (f", width {self.width}" if self.width else "")
        if k is EncodingKind.ASCII_ENUM:
            return "ASCII character data"
        if k is EncodingKind.ASCII_STRING:
            return "ASCII string"
        return f"raw bytes ({self.width or 'variable'})"


_INT_FORMATS = {1: "B", 2: "H", 4: "I", 8: "Q"}


def _endian(enc: Encoding) -> str:
    return "<" if enc.endian == "le" else ">"


def _bcd_decode(data: bytes, endian: str) -> int:
    ordered = data if endian == "be" else data[::-1]
    value = 0
    for byte in ordered:
        hi, lo = byte >> 4, byte & 0x0F
        if hi > 9 or lo > 9:
            raise EncodingError(f"invalid BCD byte 0x{byte:02x}")
        value = value * 100 + hi * 10 + lo
    return value


def _bcd_encode(value: int, width: int, endian: str) -> bytes:
    if value < 0:
        raise EncodingError("BCD cannot represent negative values")
    digits = f"{value:0{width * 2}d}"
    if len(digits) > width * 2:
        raise EncodingError(f"{value} does not fit in {width} BCD bytes")
    data = bytes(int(digits[i : i + 2], 16) for i in range(0, len(digits), 2))
    return data if endian == "be" else data[::-1]


def decode_raw_int(enc: Encoding, data: bytes) -> int:
    if len(data) != enc.width:
        raise EncodingError(f"expected {enc.width} bytes, got {len(data)}")
    if enc.kind is EncodingKind.BCD:
        return _bcd_decode(data, enc.endian)
    return int.from_bytes(data, "little" if enc.endian == "le" else "big", signed=enc.kind is EncodingKind.INT)


def _scaled(raw: int, scale_exp: int) -> int | float:
    if scale_exp == 0:
        return raw
    if scale_exp > 0:
        factor: int = 10**scale_exp
        return raw * factor
    return round(raw * 10.0**scale_exp, -scale_exp)


def decode_binary(enc: Encoding, data: bytes) -> int | float | bytes:
    if enc.kind is EncodingKind.BYTES:
        return bytes(data)
    if enc.kind is EncodingKind.FLOAT:
        if enc.width not in (4, 8) or len(data) != enc.width:
            raise EncodingError(f"float needs 4 or 8 bytes, got {len(data)}")
        fmt = _endian(enc) + ("f" if enc.width == 4 else "d")
        value = float(struct.unpack(fmt, data)[0])
        if enc.width == 4 and math.isfinite(value):
            value = shortest_float32(value)
        return value
    raw = decode_raw_int(enc, data)
    return _scaled(raw, enc.scale_exp)


def encode_binary(enc: Encoding, value: Value | bytes) -> bytes:
    if enc.kind is EncodingKind.BYTES:
        if not isinstance(value, bytes):
            raise EncodingError("bytes field needs a bytes value")
        if enc.width and len(value) != enc.width:
            raise EncodingError(f"expected {enc.width} bytes")
        return value
    if isinstance(value, bytes | str):
        raise EncodingError(f"numeric field cannot take {type(value).__name__}")
    number = float(value)
    if enc.kind is EncodingKind.FLOAT:
        fmt = _endian(enc) + ("f" if enc.width == 4 else "d")
        try:
            return struct.pack(fmt, number)
        except OverflowError as exc:
            raise EncodingError(str(exc)) from exc
    raw = round(number / 10.0**enc.scale_exp)
    if abs(raw * 10.0**enc.scale_exp - number) > 10.0**enc.scale_exp / 2 + 1e-9:
        raise EncodingError(f"{value} is not representable with scale 10^{enc.scale_exp}")
    if enc.kind is EncodingKind.BCD:
        return _bcd_encode(raw, enc.width, enc.endian)
    signed = enc.kind is EncodingKind.INT
    bits = enc.width * 8
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if not lo <= raw <= hi:
        raise EncodingError(f"{value} out of range for {enc.describe()}")
    return raw.to_bytes(enc.width, "little" if enc.endian == "le" else "big", signed=signed)


def shortest_float32(value: float) -> float:
    """Return the shortest decimal that round-trips through float32.

    Vendor UIs send decimals typed by a person; recovering 2.5 instead of
    2.5000000000 keeps comparisons with UI values exact.
    """
    packed = struct.pack("<f", value)
    for digits in range(1, 10):
        candidate = float(f"{value:.{digits}g}")
        if struct.pack("<f", candidate) == packed:
            return candidate
    return value


_NUMBER_RE = re.compile(r"^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$")
_INT_RE = re.compile(r"^\s*[+-]?\d+\s*$")
_HEX_RE = re.compile(r"^\s*[0-9A-Fa-f]+\s*$")


def is_number(text: str) -> bool:
    return bool(_NUMBER_RE.match(text))


def decode_ascii(enc: Encoding, text: str) -> Value:
    k = enc.kind
    if k is EncodingKind.ASCII_INT:
        if not _INT_RE.match(text):
            raise EncodingError(f"not an integer: {text!r}")
        return _scaled(int(text), enc.scale_exp) if enc.scale_exp else int(text)
    if k is EncodingKind.ASCII_FLOAT:
        if not _NUMBER_RE.match(text):
            raise EncodingError(f"not a number: {text!r}")
        return float(text)
    if k is EncodingKind.ASCII_HEX:
        if not _HEX_RE.match(text):
            raise EncodingError(f"not hexadecimal: {text!r}")
        return int(text, 16)
    if k is EncodingKind.ASCII_STRING:
        stripped = text.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
            return stripped[1:-1]
        return stripped
    if k is EncodingKind.ASCII_ENUM:
        return text.strip()
    raise EncodingError(f"{k} is not an ASCII encoding")


def _decimals_for(value: float, enc: Encoding) -> int:
    if enc.decimals is not None:
        return enc.decimals
    text = repr(float(value))
    if "e" in text or "E" in text:
        text = f"{value:.10f}".rstrip("0")
    places = len(text.split(".")[1]) if "." in text else 0
    return max(places, enc.min_decimals)


def encode_ascii(enc: Encoding, value: Value) -> str:
    k = enc.kind
    if k in (EncodingKind.ASCII_ENUM, EncodingKind.ASCII_STRING):
        text = str(value)
        if k is EncodingKind.ASCII_STRING:
            text = f'"{text}"'
        return text.ljust(enc.width) if enc.width else text
    if isinstance(value, str):
        raise EncodingError(f"numeric field cannot take a string: {value!r}")
    sign = "+" if enc.plus_sign else ""
    pad = "0" if enc.zero_pad else ""
    width = str(enc.width) if enc.width else ""
    if k is EncodingKind.ASCII_INT:
        raw = round(float(value) / 10.0**enc.scale_exp) if enc.scale_exp else int(value)
        text = format(raw, f"{sign}{pad}{width}d")
    elif k is EncodingKind.ASCII_HEX:
        text = format(int(value), f"{pad}{width}{'X' if enc.upper else 'x'}")
    elif k is EncodingKind.ASCII_FLOAT:
        number = float(value)
        if enc.exponent:
            places = enc.decimals if enc.decimals is not None else 6
            text = format(number, f"{sign}{pad}{width}.{places}{'E' if enc.upper else 'e'}")
        else:
            text = format(number, f"{sign}{pad}{width}.{_decimals_for(number, enc)}f")
    else:
        raise EncodingError(f"{k} is not an ASCII encoding")
    if enc.width and len(text) > enc.width:
        raise EncodingError(f"{value} does not fit in {enc.width} characters")
    return text


def decode(enc: Encoding, data: bytes | str) -> Value | bytes:
    if enc.is_binary:
        if isinstance(data, str):
            raise EncodingError("binary encoding needs bytes")
        return decode_binary(enc, data)
    text = data.decode("latin-1") if isinstance(data, bytes) else data
    return decode_ascii(enc, text)


def values_equal(a: object, b: object, *, rel: float = 1e-6) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=1e-9)
    return a == b
