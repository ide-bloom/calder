"""Incremental framers that split a byte stream into frames and build frames.

Copied verbatim into generated packages (stdlib only).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class FramingKind(StrEnum):
    DELIMITED = "delimited"
    LENGTH_PREFIXED = "length_prefixed"
    FIXED_LENGTH = "fixed_length"
    MESSAGE = "message"
    MODBUS_RTU = "modbus_rtu"


class FramingError(ValueError):
    """Bytes in the stream cannot be framed."""


@dataclass(frozen=True)
class LengthField:
    """A length field: frame_length = value + adjust."""

    offset: int
    width: int
    endian: str = "le"
    adjust: int = 0

    def read(self, data: bytes) -> int:
        raw = data[self.offset : self.offset + self.width]
        return int.from_bytes(raw, "little" if self.endian == "le" else "big") + self.adjust

    def write(self, frame: bytes) -> bytes:
        value = len(frame) - self.adjust
        if value < 0 or value >= 1 << (8 * self.width):
            raise FramingError(f"frame length {len(frame)} does not fit the length field")
        raw = value.to_bytes(self.width, "little" if self.endian == "le" else "big")
        return frame[: self.offset] + raw + frame[self.offset + self.width :]

    def describe(self) -> str:
        return (
            f"{self.width * 8}-bit {self.endian} length at offset {self.offset}; "
            f"frame length = value {'+' if self.adjust >= 0 else '-'} {abs(self.adjust)}"
        )


@dataclass(frozen=True)
class FramingSpec:
    kind: FramingKind
    terminator_tx: bytes = b""
    terminator_rx: bytes = b""
    start: bytes = b""
    length_field: LengthField | None = None
    frame_length: int = 0
    max_frame: int = 4096
    gap_ms: float = 0.0

    def terminator(self, tx: bool) -> bytes:
        return self.terminator_tx if tx else self.terminator_rx

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "terminator_tx": self.terminator_tx.hex(),
            "terminator_rx": self.terminator_rx.hex(),
            "start": self.start.hex(),
            "length_field": asdict(self.length_field) if self.length_field else None,
            "frame_length": self.frame_length,
            "max_frame": self.max_frame,
            "gap_ms": self.gap_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FramingSpec:
        lf = data.get("length_field")
        return cls(
            kind=FramingKind(data["kind"]),
            terminator_tx=bytes.fromhex(data.get("terminator_tx", "")),
            terminator_rx=bytes.fromhex(data.get("terminator_rx", "")),
            start=bytes.fromhex(data.get("start", "")),
            length_field=LengthField(**lf) if lf else None,
            frame_length=int(data.get("frame_length", 0)),
            max_frame=int(data.get("max_frame", 4096)),
            gap_ms=float(data.get("gap_ms") or 0.0),
        )


def modbus_rtu_length(data: bytes, *, request: bool) -> int | None:
    """Expected Modbus RTU frame length from the bytes seen so far, or None if unknown yet."""
    if len(data) < 2:
        return None
    func = data[1]
    if func & 0x80:
        return 5
    if func in (0x01, 0x02, 0x03, 0x04):
        if request:
            return 8
        return None if len(data) < 3 else 5 + data[2]
    if func in (0x05, 0x06):
        return 8
    if func in (0x0F, 0x10):
        if not request:
            return 8
        return None if len(data) < 7 else 9 + data[6]
    raise FramingError(f"unsupported Modbus function 0x{func:02x}")


class Framer:
    """Accumulates bytes for one direction and yields complete frames."""

    def __init__(self, spec: FramingSpec, *, tx: bool) -> None:
        self.spec = spec
        self.tx = tx
        self._buf = bytearray()

    @property
    def pending(self) -> bytes:
        return bytes(self._buf)

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[bytes]:
        spec = self.spec
        if spec.kind is FramingKind.MESSAGE:
            return [bytes(data)] if data else []
        self._buf.extend(data)
        frames: list[bytes] = []
        while True:
            frame = self._next()
            if frame is None:
                break
            frames.append(frame)
        if len(self._buf) > spec.max_frame:
            self._buf.clear()
            raise FramingError("frame exceeds maximum size without a boundary")
        return frames

    def _resync(self) -> None:
        start = self.spec.start
        if not start:
            return
        idx = self._buf.find(start)
        if idx < 0:
            keep = len(start) - 1
            del self._buf[: len(self._buf) - keep if keep else len(self._buf)]
        elif idx > 0:
            del self._buf[:idx]

    def _next(self) -> bytes | None:
        spec = self.spec
        buf = self._buf
        if spec.kind is FramingKind.DELIMITED:
            term = spec.terminator(self.tx)
            idx = buf.find(term)
            if idx < 0:
                return None
            frame = bytes(buf[: idx + len(term)])
            del buf[: idx + len(term)]
            return frame
        if spec.kind is FramingKind.FIXED_LENGTH:
            self._resync()
            if len(buf) < spec.frame_length:
                return None
            frame = bytes(buf[: spec.frame_length])
            del buf[: spec.frame_length]
            return frame
        if spec.kind is FramingKind.LENGTH_PREFIXED:
            self._resync()
            lf = spec.length_field
            if lf is None:
                raise FramingError("length-prefixed framing needs a length field")
            if len(buf) < lf.offset + lf.width:
                return None
            total = lf.read(bytes(buf))
            if total < lf.offset + lf.width or total > spec.max_frame:
                del buf[:1]
                return self._next() if buf else None
            if len(buf) < total:
                return None
            frame = bytes(buf[:total])
            del buf[:total]
            return frame
        if spec.kind is FramingKind.MODBUS_RTU:
            expected = modbus_rtu_length(bytes(buf), request=self.tx)
            if expected is None or len(buf) < expected:
                return None
            frame = bytes(buf[:expected])
            del buf[:expected]
            return frame
        raise FramingError(f"unsupported framing {spec.kind}")


def wrap(spec: FramingSpec, body: bytes, *, tx: bool) -> bytes:
    """Add framing that is not part of the message layout (terminators)."""
    if spec.kind is FramingKind.DELIMITED:
        return body + spec.terminator(tx)
    return body


def unwrap(spec: FramingSpec, frame: bytes, *, tx: bool) -> bytes:
    if spec.kind is FramingKind.DELIMITED:
        term = spec.terminator(tx)
        if term and frame.endswith(term):
            return frame[: -len(term)]
    return frame


def split(spec: FramingSpec, data: bytes, *, tx: bool) -> tuple[list[bytes], bytes]:
    """Frame a complete buffer; returns frames and any trailing partial frame."""
    framer = Framer(spec, tx=tx)
    frames = framer.feed(data)
    return frames, framer.pending
