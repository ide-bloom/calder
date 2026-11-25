"""The protocol model: Calder's versioned, evidence-linked description of a protocol.

Every inferred element carries an :class:`Evidence` record naming the method
that produced it, the events that support it, a confidence and the
alternatives that were considered and rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from calder.session.models import Direction, Transport

SCHEMA_VERSION = "1.0"
MODEL_KIND: Literal["calder.protocol"] = "calder.protocol"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Status(StrEnum):
    INFERRED = "inferred"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EDITED = "edited"


class EventRef(_Model):
    session: str
    index: int
    ui: bool = False


class Alternative(_Model):
    description: str
    score: float = 0.0
    reason: str = ""


class Evidence(_Model):
    method: str
    confidence: float = Field(ge=0.0, le=1.0)
    support: int = 0
    contradictions: int = 0
    events: list[EventRef] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    notes: str = ""

    @property
    def uncertain(self) -> bool:
        return self.confidence < 0.8


class SerialSettings(_Model):
    baudrate: int | None = None
    bytesize: int = 8
    parity: Literal["N", "E", "O", "M", "S"] = "N"
    stopbits: float = 1.0
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False
    read_timeout_ms: int | None = None
    write_timeout_ms: int | None = None


class TransportSpec(_Model):
    kind: Transport
    resource: str = ""
    serial: SerialSettings | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    evidence: Evidence


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


class EncodingModel(_Model):
    kind: EncodingKind
    width: int = 0
    endian: Literal["le", "be"] = "le"
    scale_exp: int = 0
    decimals: int | None = None
    min_decimals: int = 0
    plus_sign: bool = False
    zero_pad: bool = False
    exponent: bool = False
    upper: bool = True


class FieldRole(StrEnum):
    VALUE = "value"
    ENUM = "enum"
    COUNTER = "counter"
    CONSTANT = "constant"
    ADDRESS = "address"
    STATUS = "status"
    ECHO = "echo"
    OPCODE = "opcode"
    LENGTH = "length"
    CHECKSUM = "checksum"
    START = "start"


class EnumValue(_Model):
    wire: str
    label: str
    ui_value: str | None = None
    count: int = 0


class ValueRange(_Model):
    minimum: float
    maximum: float


class FieldModel(_Model):
    id: str
    name: str
    role: FieldRole
    offset: int | None = None
    length: int | None = None
    token: int | None = None
    encoding: EncodingModel | None = None
    constant: str | None = Field(default=None, description="Hex for binary layouts, text for ASCII")
    enum: list[EnumValue] = Field(default_factory=list)
    units: str | None = None
    label: str | None = None
    ui_element: str | None = None
    range: ValueRange | None = None
    echo_of: str | None = None
    reflects: tuple[str, str] | None = Field(default=None, description="(message id, field id)")
    status_ok: list[str] = Field(default_factory=list)
    status: Status = Status.INFERRED
    suggested_name: str | None = None
    suggested_doc: str | None = None
    doc: str = ""
    evidence: Evidence


class MessageKind(StrEnum):
    COMMAND = "command"
    QUERY = "query"
    RESPONSE = "response"
    UNSOLICITED = "unsolicited"


class Classification(StrEnum):
    READ_ONLY = "read_only"
    CONFIGURING = "configuring"
    ACTUATING = "actuating"


class UiBinding(_Model):
    element: str
    element_name: str = ""
    kind: str
    constants: dict[str, str] = Field(default_factory=dict, description="field id -> wire value")
    support: int = 0
    confidence: float = 0.0


class Precondition(_Model):
    message: str
    disabled_by: list[str] = Field(default_factory=list)
    status: Status = Status.INFERRED
    evidence: Evidence


class Example(_Model):
    session: str
    index: int
    raw: str = Field(description="Hex of the full frame")
    text: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class ManualRef(_Model):
    mnemonic: str
    description: str
    page: int | None = None
    score: float = 0.0


class MessageType(_Model):
    id: str
    name: str
    direction: Direction
    kind: MessageKind
    key: str
    layout: Literal["tokens", "columns", "binary"]
    header: str = ""
    header_sep: str = " "
    arg_sep: str = ","
    length: int | None = None
    fields: list[FieldModel] = Field(default_factory=list)
    response: str | None = None
    response_timeout_ms: int = 1000
    classification: Classification = Classification.READ_ONLY
    classification_reason: str = ""
    classification_confirmed: bool = False
    ui_bindings: list[UiBinding] = Field(default_factory=list)
    preconditions: list[Precondition] = Field(default_factory=list)
    examples: list[Example] = Field(default_factory=list)
    count: int = 0
    status: Status = Status.INFERRED
    suggested_name: str | None = None
    suggested_doc: str | None = None
    doc: str = ""
    manual: ManualRef | None = None
    evidence: Evidence

    def field(self, field_id: str) -> FieldModel:
        for f in self.fields:
            if f.id == field_id:
                return f
        raise KeyError(field_id)


class FramingKind(StrEnum):
    DELIMITED = "delimited"
    LENGTH_PREFIXED = "length_prefixed"
    FIXED_LENGTH = "fixed_length"
    MESSAGE = "message"
    MODBUS_RTU = "modbus_rtu"


class LengthFieldModel(_Model):
    offset: int
    width: int
    endian: Literal["le", "be"] = "le"
    adjust: int = 0
    includes_header: bool = False


class FramingModel(_Model):
    kind: FramingKind
    encoding: Literal["ascii", "binary"]
    terminator_tx: str = Field(default="", description="Hex")
    terminator_rx: str = Field(default="", description="Hex")
    start: str = Field(default="", description="Hex")
    length_field: LengthFieldModel | None = None
    frame_length: int = 0
    fixed_width_rx: int | None = None
    fixed_width_tx: int | None = None
    gap_ms: float | None = None
    evidence: Evidence


class ChecksumModel(_Model):
    algorithm: str
    width: int
    start: int
    end: int
    position: int
    endian: Literal["le", "be"] = "be"
    encoding: Literal["raw", "ascii_hex"] = "raw"
    covered: Literal["raw", "hex"] = Field(default="raw", description="'hex' when coverage is hex text decoded first")
    poly: int | None = None
    init: int | None = None
    refin: bool | None = None
    refout: bool | None = None
    xorout: int | None = None
    evidence: Evidence


class ProtocolFamily(StrEnum):
    GENERIC = "generic"
    SCPI = "scpi"
    MODBUS_RTU = "modbus_rtu"
    MODBUS_ASCII = "modbus_ascii"
    MODBUS_TCP = "modbus_tcp"


class StateModel(_Model):
    id: int
    label: str = ""


class TransitionModel(_Model):
    source: int
    target: int
    message: str
    count: int = 0


class StateMachineModel(_Model):
    k: int
    initial: int = 0
    states: list[StateModel] = Field(default_factory=list)
    transitions: list[TransitionModel] = Field(default_factory=list)
    evidence: Evidence


class Exchange(_Model):
    session: str
    index: int
    request: str
    responses: list[str] = Field(default_factory=list)
    message: str
    response_message: str | None = None
    latency_ms: float = 0.0


class SessionRef(_Model):
    id: str
    path: str = ""
    sha256: str = ""
    events: int = 0
    ui_events: int = 0
    source: str = ""


class AuditEntry(_Model):
    time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: str = "user"
    action: str
    target: str
    before: Any = None
    after: Any = None
    note: str = ""


class ManualInfo(_Model):
    path: str
    sha256: str = ""
    pages: int = 0
    commands_found: int = 0


class ProtocolModel(_Model):
    kind: Literal["calder.protocol"] = MODEL_KIND
    schema_version: str = SCHEMA_VERSION
    name: str
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    calder_version: str = ""
    family: ProtocolFamily = ProtocolFamily.GENERIC
    stream: str = ""
    sessions: list[SessionRef] = Field(default_factory=list)
    transport: TransportSpec
    framing: FramingModel
    checksum: ChecksumModel | None = None
    messages: list[MessageType] = Field(default_factory=list)
    state_machine: StateMachineModel | None = None
    exchanges: list[Exchange] = Field(default_factory=list)
    unsolicited: list[str] = Field(default_factory=list, description="Hex frames streamed by the instrument")
    warnings: list[str] = Field(default_factory=list)
    manual: ManualInfo | None = None
    audit: list[AuditEntry] = Field(default_factory=list)

    def message(self, message_id: str) -> MessageType:
        for m in self.messages:
            if m.id == message_id:
                return m
        raise KeyError(message_id)

    def active_messages(self) -> list[MessageType]:
        return [m for m in self.messages if m.status is not Status.REJECTED]
