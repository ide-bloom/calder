"""Runtime protocol description and the generic message codec.

A generated driver embeds ``protocol.json`` (produced from Calder's protocol
model) and drives everything through this codec, so the bytes a driver sends
are built by exactly the same rules that inference validated.

Copied verbatim into generated packages (stdlib only).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checksums import ChecksumSpec
from .encodings import (
    Encoding,
    EncodingError,
    EncodingKind,
    Value,
    decode,
    encode_ascii,
    encode_binary,
)
from .framing import FramingKind, FramingSpec, unwrap, wrap

RUNTIME_FORMAT = "calder.runtime/1"


class CodecError(ValueError):
    """A frame does not match any known message, or values cannot be encoded."""


def normalize_header(text: str) -> str:
    """Canonical form of an ASCII command header (SCPI is case-insensitive)."""
    return text.strip().lstrip(":").upper()


def split_args(text: str, sep: str) -> list[str]:
    """Split arguments on ``sep`` outside of quoted strings."""
    if not text:
        return []
    if not sep:
        return [text]
    out: list[str] = []
    current: list[str] = []
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            current.append(ch)
        elif text.startswith(sep, i):
            out.append("".join(current).strip())
            current = []
            i += len(sep)
            continue
        else:
            current.append(ch)
        i += 1
    out.append("".join(current).strip())
    return out


@dataclass(frozen=True)
class FieldSpec:
    id: str
    name: str
    role: str
    offset: int | None = None
    length: int | None = None
    token: int | None = None
    encoding: Encoding | None = None
    constant: str | None = None
    enum: dict[str, str] = field(default_factory=dict)
    units: str | None = None
    label: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    echo_of: str | None = None
    reflects: tuple[str, str] | None = None
    status_ok: tuple[str, ...] = ()
    doc: str = ""

    @property
    def is_input(self) -> bool:
        """True when the caller supplies this field's value."""
        return self.role in ("value", "enum")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FieldSpec:
        enc = data.get("encoding")
        reflects = data.get("reflects")
        return cls(
            id=data["id"],
            name=data["name"],
            role=data["role"],
            offset=data.get("offset"),
            length=data.get("length"),
            token=data.get("token"),
            encoding=Encoding.from_dict(enc) if enc else None,
            constant=data.get("constant"),
            enum=dict(data.get("enum") or {}),
            units=data.get("units"),
            label=data.get("label"),
            minimum=data.get("minimum"),
            maximum=data.get("maximum"),
            echo_of=data.get("echo_of"),
            reflects=(reflects[0], reflects[1]) if reflects else None,
            status_ok=tuple(data.get("status_ok") or ()),
            doc=data.get("doc", ""),
        )


@dataclass(frozen=True)
class PreconditionSpec:
    message: str
    disabled_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class MessageSpec:
    id: str
    name: str
    direction: str
    kind: str
    key: str
    layout: str
    header: str = ""
    header_sep: str = " "
    arg_sep: str = ","
    length: int | None = None
    fields: tuple[FieldSpec, ...] = ()
    response: str | None = None
    response_timeout_ms: int = 1000
    classification: str = "read_only"
    preconditions: tuple[PreconditionSpec, ...] = ()
    doc: str = ""

    @property
    def inputs(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if f.is_input)

    def field_by_name(self, name: str) -> FieldSpec:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MessageSpec:
        return cls(
            id=data["id"],
            name=data["name"],
            direction=data["direction"],
            kind=data["kind"],
            key=data["key"],
            layout=data["layout"],
            header=data.get("header", ""),
            header_sep=data.get("header_sep", " "),
            arg_sep=data.get("arg_sep", ","),
            length=data.get("length"),
            fields=tuple(FieldSpec.from_dict(f) for f in data.get("fields", [])),
            response=data.get("response"),
            response_timeout_ms=int(data.get("response_timeout_ms", 1000)),
            classification=data.get("classification", "read_only"),
            preconditions=tuple(
                PreconditionSpec(p["message"], tuple(p.get("disabled_by", ()))) for p in data.get("preconditions", [])
            ),
            doc=data.get("doc", ""),
        )


@dataclass(frozen=True)
class ExchangeSpec:
    session: str
    index: int
    request: bytes
    responses: tuple[bytes, ...]
    message: str
    response_message: str | None
    latency_ms: float


@dataclass(frozen=True)
class ProtocolSpec:
    name: str
    family: str
    encoding: str
    transport: dict[str, Any]
    framing: FramingSpec
    checksum: ChecksumSpec | None
    messages: tuple[MessageSpec, ...]
    exchanges: tuple[ExchangeSpec, ...] = ()
    unsolicited: tuple[bytes, ...] = ()
    model_sha256: str = ""

    def message(self, message_id: str) -> MessageSpec:
        for m in self.messages:
            if m.id == message_id:
                return m
        raise KeyError(message_id)

    def by_name(self, name: str) -> MessageSpec:
        for m in self.messages:
            if m.name == name:
                return m
        raise KeyError(name)

    def tx_messages(self) -> tuple[MessageSpec, ...]:
        return tuple(m for m in self.messages if m.direction == "tx")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProtocolSpec:
        if data.get("format") != RUNTIME_FORMAT:
            raise CodecError(f"unsupported runtime protocol format {data.get('format')!r}")
        ck = data.get("checksum")
        return cls(
            name=data["name"],
            family=data["family"],
            encoding=data["encoding"],
            transport=dict(data["transport"]),
            framing=FramingSpec.from_dict(data["framing"]),
            checksum=ChecksumSpec.from_dict(ck) if ck else None,
            messages=tuple(MessageSpec.from_dict(m) for m in data["messages"]),
            exchanges=tuple(
                ExchangeSpec(
                    session=e["session"],
                    index=int(e["index"]),
                    request=bytes.fromhex(e["request"]),
                    responses=tuple(bytes.fromhex(r) for r in e["responses"]),
                    message=e["message"],
                    response_message=e.get("response_message"),
                    latency_ms=float(e.get("latency_ms", 0.0)),
                )
                for e in data.get("exchanges", [])
            ),
            unsolicited=tuple(bytes.fromhex(u) for u in data.get("unsolicited", [])),
            model_sha256=data.get("model_sha256", ""),
        )

    @classmethod
    def load(cls, path: str | Path) -> ProtocolSpec:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass
class Decoded:
    message: MessageSpec
    values: dict[str, Any]
    raw: bytes

    @property
    def id(self) -> str:
        return self.message.id


def _const_bytes(f: FieldSpec) -> bytes:
    return bytes.fromhex(f.constant or "")


class Codec:
    """Encodes and decodes frames for one protocol."""

    def __init__(self, spec: ProtocolSpec) -> None:
        self.spec = spec
        self._counters: dict[str, int] = {}

    # ----- encoding -------------------------------------------------------------

    def encode(self, message: MessageSpec | str, values: Mapping[str, Any] | None = None) -> bytes:
        msg = self.spec.message(message) if isinstance(message, str) else message
        vals = dict(values or {})
        tx = msg.direction == "tx"
        if msg.layout == "binary":
            body = self._encode_binary(msg, vals)
        else:
            text = self._encode_columns(msg, vals) if msg.layout == "columns" else self._encode_tokens(msg, vals)
            body = text.encode("latin-1")
            if self.spec.checksum is not None:
                body = self.spec.checksum.apply(body)
        return wrap(self.spec.framing, body, tx=tx)

    def _field_value(self, msg: MessageSpec, f: FieldSpec, vals: dict[str, Any]) -> Any:
        if f.name in vals:
            value = vals[f.name]
            if hasattr(value, "value") and not isinstance(value, int | float | str | bytes):
                value = value.value
            return value
        if f.role == "counter":
            width = f.length or 1
            current = self._counters.get(msg.direction, -1) + 1
            current %= 1 << (8 * width)
            self._counters[msg.direction] = current
            return current
        if f.constant is not None:
            return None
        raise CodecError(f"{msg.name}: missing value for field {f.name!r}")

    def _encode_tokens(self, msg: MessageSpec, vals: dict[str, Any]) -> str:
        args: list[tuple[int, str]] = []
        for f in msg.fields:
            if f.token is None:
                continue
            value = self._field_value(msg, f, vals)
            if value is None:
                text = f.constant or ""
            elif f.encoding is None:
                text = str(value)
            else:
                text = self._ascii(f, value)
            args.append((f.token, text))
        args.sort()
        joined = msg.arg_sep.join(text for _, text in args)
        if not msg.header:
            return joined
        if not args:
            return msg.header
        return f"{msg.header}{msg.header_sep}{joined}"

    def _ascii(self, f: FieldSpec, value: Any) -> str:
        if f.encoding is None:
            return str(value)
        if f.role == "enum" and f.encoding.kind is EncodingKind.ASCII_ENUM:
            text = str(value)
            if f.enum and text not in f.enum:
                raise CodecError(f"{f.name}: {text!r} is not one of {sorted(f.enum)}")
            return text
        try:
            return encode_ascii(f.encoding, value)
        except EncodingError as exc:
            raise CodecError(f"{f.name}: {exc}") from exc

    def _encode_columns(self, msg: MessageSpec, vals: dict[str, Any]) -> str:
        width = max(((f.offset or 0) + (f.length or 0) for f in msg.fields), default=0)
        chars = [" "] * width
        for f in msg.fields:
            if f.offset is None or f.length is None or f.role == "checksum":
                continue
            value = self._field_value(msg, f, vals)
            text = (f.constant or "") if value is None else self._ascii(f, value)
            if len(text) != f.length:
                text = text.rjust(f.length) if f.encoding and f.encoding.is_numeric else text.ljust(f.length)
            if len(text) != f.length:
                raise CodecError(f"{f.name}: {text!r} does not fit {f.length} columns")
            chars[f.offset : f.offset + f.length] = list(text)
        return "".join(chars)

    def _encode_binary(self, msg: MessageSpec, vals: dict[str, Any]) -> bytes:
        size = msg.length or max(((f.offset or 0) + (f.length or 0) for f in msg.fields), default=0)
        frame = bytearray(size)
        for f in msg.fields:
            if f.offset is None or f.length is None or f.role in ("length", "checksum"):
                continue
            value = self._field_value(msg, f, vals)
            if value is None:
                data = _const_bytes(f)
            else:
                if f.encoding is None:
                    raise CodecError(f"{f.name}: no encoding")
                try:
                    data = encode_binary(f.encoding, value)
                except EncodingError as exc:
                    raise CodecError(f"{f.name}: {exc}") from exc
            if len(data) != f.length:
                raise CodecError(f"{f.name}: encoded {len(data)} bytes, expected {f.length}")
            frame[f.offset : f.offset + f.length] = data
        out = bytes(frame)
        lf = self.spec.framing.length_field
        if lf is not None and self.spec.framing.kind is FramingKind.LENGTH_PREFIXED:
            out = lf.write(out)
        if self.spec.checksum is not None:
            out = self.spec.checksum.apply(out)
        return out

    # ----- decoding -------------------------------------------------------------

    def candidates(self, direction: str) -> list[MessageSpec]:
        return [m for m in self.spec.messages if m.direction == direction]

    def decode(self, frame: bytes, *, direction: str, expected: str | None = None) -> Decoded:
        """Identify and decode a frame. ``expected`` names the message to prefer."""
        options = self.candidates(direction)
        if expected is not None:
            options.sort(key=lambda m: m.id != expected)
        errors: list[str] = []
        for msg in options:
            if not msg.header and not self._has_constants(msg) and msg.id != expected:
                continue
            try:
                return self.decode_as(msg, frame)
            except (CodecError, EncodingError, ValueError) as exc:
                errors.append(f"{msg.id}: {exc}")
        raise CodecError(f"frame {frame!r} matches no {direction} message ({'; '.join(errors[:4])})")

    @staticmethod
    def _has_constants(msg: MessageSpec) -> bool:
        return any(f.constant for f in msg.fields)

    def decode_as(self, msg: MessageSpec, frame: bytes) -> Decoded:
        tx = msg.direction == "tx"
        body = unwrap(self.spec.framing, frame, tx=tx)
        if self.spec.checksum is not None:
            checked = frame if msg.layout == "binary" else body
            if not self.spec.checksum.verify(checked):
                raise CodecError("checksum mismatch")
        if msg.layout == "binary":
            values = self._decode_binary(msg, body)
        elif msg.layout == "columns":
            values = self._decode_columns(msg, body.decode("latin-1"))
        else:
            values = self._decode_tokens(msg, body.decode("latin-1"))
        return Decoded(message=msg, values=values, raw=frame)

    def _check_enum(self, f: FieldSpec, value: Any) -> None:
        if f.role == "enum" and f.enum and str(value) not in f.enum:
            raise CodecError(f"{f.name}: unexpected value {value!r}")

    def _decode_tokens(self, msg: MessageSpec, text: str) -> dict[str, Any]:
        rest = text
        if msg.header:
            if msg.header_sep:
                head, _, rest = text.partition(msg.header_sep)
                if normalize_header(head) != normalize_header(msg.header):
                    raise CodecError(f"header {head!r} != {msg.header!r}")
            else:
                if not normalize_header(text).startswith(normalize_header(msg.header)):
                    raise CodecError(f"header mismatch for {msg.header!r}")
                rest = text.strip().lstrip(":")[len(msg.header.lstrip(":")) :]
        args = split_args(rest, msg.arg_sep)
        token_fields = [f for f in msg.fields if f.token is not None]
        if len(args) != len(token_fields):
            raise CodecError(f"expected {len(token_fields)} arguments, got {len(args)}")
        values: dict[str, Any] = {}
        for f in token_fields:
            assert f.token is not None
            raw = args[f.token]
            if f.constant is not None and f.role == "constant":
                if normalize_header(raw) != normalize_header(f.constant):
                    raise CodecError(f"{f.name}: {raw!r} != constant {f.constant!r}")
                continue
            value = raw if f.encoding is None else decode(f.encoding, raw)
            self._check_enum(f, value)
            values[f.name] = value
        return values

    def _decode_columns(self, msg: MessageSpec, text: str) -> dict[str, Any]:
        width = max(((f.offset or 0) + (f.length or 0) for f in msg.fields), default=0)
        if len(text) != width:
            raise CodecError(f"expected {width} characters, got {len(text)}")
        values: dict[str, Any] = {}
        for f in msg.fields:
            if f.offset is None or f.length is None:
                continue
            raw = text[f.offset : f.offset + f.length]
            if f.role == "checksum":
                continue
            if f.role == "constant":
                if raw != (f.constant or ""):
                    raise CodecError(f"{f.name}: {raw!r} != {f.constant!r}")
                continue
            value: Any = raw.strip() if f.encoding is None else decode(f.encoding, raw.strip())
            self._check_enum(f, value)
            values[f.name] = value
        return values

    def _decode_binary(self, msg: MessageSpec, frame: bytes) -> dict[str, Any]:
        if msg.length is not None and len(frame) != msg.length:
            raise CodecError(f"expected {msg.length} bytes, got {len(frame)}")
        values: dict[str, Any] = {}
        for f in msg.fields:
            if f.offset is None or f.length is None:
                continue
            raw = frame[f.offset : f.offset + f.length]
            if len(raw) != f.length:
                raise CodecError(f"{f.name}: frame too short")
            if f.role in ("constant", "address", "opcode", "start"):
                if raw != _const_bytes(f):
                    raise CodecError(f"{f.name}: {raw.hex()} != {f.constant}")
                continue
            if f.role in ("length", "checksum"):
                continue
            if f.encoding is None:
                values[f.name] = raw
                continue
            value = decode(f.encoding, raw)
            self._check_enum(f, value)
            values[f.name] = value
        return values


def load_embedded(package_dir: str | Path) -> ProtocolSpec:
    return ProtocolSpec.load(Path(package_dir) / "protocol.json")


def value_repr(value: Value | bytes) -> str:
    if isinstance(value, bytes):
        return value.hex()
    return str(value)
