"""Pydantic models for recorded sessions.

A session is the raw, uninterpreted record of what crossed the wire and what the
user did in the vendor UI. Inference never mutates it; every inferred fact points
back into a session by event index.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

SESSION_FORMAT_VERSION = "1.0"


class Transport(StrEnum):
    SERIAL = "serial"
    VISA = "visa"
    FTDI = "ftdi"
    WINUSB = "winusb"
    HID = "hid"
    TCP = "tcp"


class Direction(StrEnum):
    """Direction relative to the host: TX is host to instrument, RX is instrument to host."""

    TX = "tx"
    RX = "rx"
    NONE = "none"


class EventKind(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    DATA = "data"
    CONFIG = "config"
    SIGNAL = "signal"
    ERROR = "error"
    INFO = "info"


class Source(StrEnum):
    HOOK = "hook"
    TAP = "tap"
    PCAPNG = "pcapng"
    SYNTHETIC = "synthetic"


class WireEvent(BaseModel):
    """One observation at the transport boundary."""

    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=0)
    t_ns: int = Field(ge=0, description="Nanoseconds since session start")
    transport: Transport
    stream: str = Field(description="Resource identity, e.g. 'serial:COM5' or 'tcp:10.0.0.5:5025'")
    direction: Direction = Direction.NONE
    kind: EventKind = EventKind.DATA
    data: bytes = b""
    connection: int = Field(default=0, ge=0, description="Increments each time the resource is reopened")
    pid: int = 0
    tid: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_serializer("data")
    def _ser_data(self, value: bytes) -> str:
        return value.hex()

    @field_validator("data", mode="before")
    @classmethod
    def _parse_data(cls, value: object) -> bytes:
        if isinstance(value, bytes | bytearray | memoryview):
            return bytes(value)
        if isinstance(value, str):
            return bytes.fromhex(value)
        raise TypeError("data must be bytes or a hex string")


class UiKind(StrEnum):
    INVOKE = "invoke"
    VALUE = "value"
    SELECTION = "selection"
    TOGGLE = "toggle"
    FOCUS = "focus"
    WINDOW = "window"


class UiElement(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = ""
    automation_id: str = ""
    control_type: str = ""
    label: str = Field(default="", description="Text of the nearest label, or LabeledBy target")
    window: str = ""

    @property
    def key(self) -> str:
        """Stable identity used to group events from the same control."""
        return self.automation_id or self.label or self.name


class UiEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=0)
    t_ns: int = Field(ge=0)
    kind: UiKind
    element: UiElement
    value: str | None = None


class AppInfo(BaseModel):
    path: str = ""
    args: list[str] = Field(default_factory=list)
    bitness: int | None = None


class SessionMeta(BaseModel):
    format_version: str = SESSION_FORMAT_VERSION
    session_id: str
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    calder_version: str = ""
    source: Source = Source.HOOK
    app: AppInfo = Field(default_factory=AppInfo)
    transports: list[Transport] = Field(default_factory=list)
    filter: str = ""
    host: str = ""
    clock: str = Field(default="qpc", description="Clock used for t_ns; both streams share it")
    dropped_events: int = 0
    description: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class Session(BaseModel):
    meta: SessionMeta
    events: list[WireEvent] = Field(default_factory=list)
    ui_events: list[UiEvent] = Field(default_factory=list)
    notes: str = ""

    def data_events(self) -> list[WireEvent]:
        return [e for e in self.events if e.kind is EventKind.DATA and e.data]

    def streams(self) -> list[str]:
        seen: dict[str, None] = {}
        for e in self.events:
            seen.setdefault(e.stream, None)
        return list(seen)
