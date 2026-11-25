"""End-to-end inference: sessions in, evidence-linked protocol model out."""

from __future__ import annotations

import itertools
import keyword
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from calder import __version__
from calder.infer import checksum as checksum_search
from calder.infer import manual as manual_assist
from calder.infer.align import field_spans, progressive_align
from calder.infer.classify import classify
from calder.infer.correlate import (
    UiContext,
    correlate_enum,
    correlate_values,
    correlator,
    invoke_bindings,
    parse_label,
    snake,
)
from calder.infer.fields import Segment, binary_candidates, segment_region, type_tokens
from calder.infer.framing import (
    detect_terminator,
    fixed_length,
    gap_threshold_ns,
    order_frames,
    search_length_field,
    split_by_length,
    split_delimited,
    split_fixed,
    split_messages,
    stream_groups,
)
from calder.infer.modbus import (
    recognize_ascii,
    recognize_rtu,
    recognize_tcp,
    request_key,
    request_name,
)
from calder.infer.pairing import pair
from calder.infer.statemachine import k_tails, preconditions
from calder.infer.streams import Chunk, Frame, StreamSet, collect, is_ascii, printable_ratio
from calder.infer.tokenize import (
    IEEE488_COMMON,
    Tokens,
    generic_tokens,
    is_scpi_like,
    loose_signature,
    scpi_tokens,
    shape_signature,
    shape_tokens,
)
from calder.model.io import to_runtime, validate_model
from calder.model.schema import (
    Alternative,
    ChecksumModel,
    Classification,
    EncodingKind,
    EncodingModel,
    EnumValue,
    EventRef,
    Evidence,
    Example,
    Exchange,
    FieldModel,
    FieldRole,
    FramingKind,
    FramingModel,
    LengthFieldModel,
    ManualInfo,
    ManualRef,
    MessageKind,
    MessageType,
    Precondition,
    ProtocolFamily,
    ProtocolModel,
    SerialSettings,
    SessionRef,
    StateMachineModel,
    StateModel,
    TransitionModel,
    TransportSpec,
    UiBinding,
    ValueRange,
)
from calder.runtime.checksums import ChecksumSpec, spec_for
from calder.runtime.encodings import Encoding, decode
from calder.runtime.protocol import Codec, CodecError, ProtocolSpec, normalize_header, split_args
from calder.session.models import Direction, EventKind, Session, Transport

TX, RX = Direction.TX, Direction.RX
MAX_EXAMPLES = 5
MAX_EXCHANGES = 5000


class InferenceError(ValueError):
    """The sessions do not contain enough structure to infer a protocol."""


@dataclass
class InferOptions:
    name: str | None = None
    stream: str | None = None
    k: int = 2
    manual: str | None = None
    session_paths: list[str] = field(default_factory=list)
    session_digests: list[str] = field(default_factory=list)


@dataclass
class Draft:
    id: str
    key: str
    direction: Direction
    layout: str
    frames: list[Frame]
    header: str = ""
    header_sep: str = " "
    arg_sep: str = ","
    tokens: list[Tokens] = field(default_factory=list)
    fields: list[FieldModel] = field(default_factory=list)
    request: str | None = None
    response: str | None = None
    unsolicited: bool = False
    answered_rate: float = 0.0
    latencies: list[int] = field(default_factory=list)
    name: str = ""
    method: str = ""
    confidence: float = 1.0
    notes: str = ""
    scpi_query: bool | None = None
    bindings: list[UiBinding] = field(default_factory=list)
    preconditions: list[Precondition] = field(default_factory=list)

    @property
    def tx(self) -> bool:
        return self.direction is TX

    def has_inputs(self) -> bool:
        return any(f.role in (FieldRole.VALUE, FieldRole.ENUM) for f in self.fields) and self.tx


class _Ctx:
    def __init__(self, sessions: Sequence[Session], streams: StreamSet) -> None:
        self.sessions = list(sessions)
        self.streams = streams
        self.ui = UiContext(sessions)
        self.warnings: list[str] = []

    def refs(self, frames: Sequence[Frame], limit: int = 20) -> list[EventRef]:
        out: list[EventRef] = []
        for f in frames[:limit]:
            out.append(EventRef(session=self.sessions[f.session].meta.session_id, index=f.events[0]))
        return out

    def ui_refs(self, pairs: Sequence[tuple[int, int]]) -> list[EventRef]:
        return [EventRef(session=self.sessions[s].meta.session_id, index=i, ui=True) for s, i in pairs[:20]]

    def session_id(self, frame: Frame) -> str:
        return self.sessions[frame.session].meta.session_id


def _ev(method: str, confidence: float, support: int = 0, **kw: Any) -> Evidence:
    return Evidence(method=method, confidence=max(0.0, min(1.0, confidence)), support=support, **kw)


def _body(f: Frame, terminator: bytes) -> str:
    raw = f.raw[: -len(terminator)] if terminator and f.raw.endswith(terminator) else f.raw
    return raw.decode("latin-1")


# ----- transport -----------------------------------------------------------------------


_SERIAL_KEYS = ("baudrate", "bytesize", "parity", "stopbits", "xonxoff", "rtscts", "dsrdtr")


def detect_transport(streams: StreamSet, sessions: Sequence[Session]) -> TransportSpec:
    kind = streams.transport
    configs: dict[str, Any] = {}
    refs: list[EventRef] = []
    settings: dict[str, Any] = {}
    for si, s in enumerate(sessions):
        name = streams.names[si] if si < len(streams.names) else streams.stream
        for e in s.events:
            if e.stream != name or e.kind not in (EventKind.OPEN, EventKind.CONFIG):
                continue
            refs.append(EventRef(session=s.meta.session_id, index=e.index))
            for k, v in e.meta.items():
                if k in _SERIAL_KEYS or k.endswith("_timeout_ms"):
                    configs[k] = v
                elif k in (
                    "vid",
                    "pid",
                    "report_size",
                    "endpoint_in",
                    "endpoint_out",
                    "interface",
                    "serial_number",
                    "description",
                    "device_kind",
                    "path",
                ):
                    settings[k] = v
    resource = streams.stream.split(":", 1)[1] if ":" in streams.stream else streams.stream
    serial: SerialSettings | None = None
    if kind in (Transport.SERIAL, Transport.FTDI) or (kind is Transport.VISA and configs):
        parity = str(configs.get("parity", "N"))[:1].upper()
        serial = SerialSettings(
            baudrate=int(configs["baudrate"]) if configs.get("baudrate") else None,
            bytesize=int(configs.get("bytesize", 8)),
            parity=parity if parity in "NEOMS" else "N",
            stopbits=float(configs.get("stopbits", 1)),
            xonxoff=bool(configs.get("xonxoff", False)),
            rtscts=bool(configs.get("rtscts", False)),
            dsrdtr=bool(configs.get("dsrdtr", False)),
            read_timeout_ms=configs.get("read_timeout_ms"),
            write_timeout_ms=configs.get("write_timeout_ms"),
        )
    confidence = 1.0 if configs or kind not in (Transport.SERIAL, Transport.FTDI) else 0.6
    notes = "" if configs else "no line-setting events were captured; defaults apply"
    return TransportSpec(
        kind=kind,
        resource=resource,
        serial=serial,
        settings=settings,
        evidence=_ev("transport.events", confidence, len(refs), events=refs[:20], notes=notes),
    )


# ----- naming helpers ----------------------------------------------------------------


def _ident(text: str) -> str:
    name = snake(text)
    if keyword.iskeyword(name) or name in ("self", "values", "timeout", "confirm"):
        name += "_"
    return name


def _unique(name: str, used: set[str]) -> str:
    candidate = name
    n = 2
    while candidate in used:
        candidate = f"{name}_{n}"
        n += 1
    used.add(candidate)
    return candidate


# ----- ASCII -----------------------------------------------------------------------------


def _ascii(
    ctx: _Ctx, tx_groups: list[list[Chunk]], rx_groups: list[list[Chunk]]
) -> tuple[ProtocolFamily, FramingModel, ChecksumModel | None, list[Draft], list[Frame]]:
    term_tx = detect_terminator(tx_groups)
    term_rx = detect_terminator(rx_groups) if any(rx_groups) else None
    if term_tx is None and term_rx is None:
        raise InferenceError("ASCII stream without a consistent terminator")
    t_tx = term_tx.terminator if term_tx else term_rx.terminator  # type: ignore[union-attr]
    t_rx = term_rx.terminator if term_rx else t_tx
    tx = split_delimited(tx_groups, t_tx, TX)
    rx = split_delimited(rx_groups, t_rx, RX)
    frames = order_frames(tx + rx)
    alts = [
        Alternative(description=f"terminator {t.hex()}", score=score, reason=f"{count} occurrences")
        for t, score, count in (term_tx.alternatives if term_tx else [])
    ]
    conf = min(term_tx.chunk_end_ratio if term_tx else 0.8, term_rx.chunk_end_ratio if term_rx else 1.0)
    framing = FramingModel(
        kind=FramingKind.DELIMITED,
        encoding="ascii",
        terminator_tx=t_tx.hex(),
        terminator_rx=t_rx.hex(),
        evidence=_ev(
            "framing.terminator",
            conf,
            len(frames),
            events=ctx.refs(frames),
            alternatives=alts,
            notes=f"{conf:.0%} of reads/writes end with the terminator",
        ),
    )
    if recognize_ascii([f.raw for f in frames]):
        return _modbus_ascii(ctx, framing, frames, t_tx, t_rx)

    texts = {f.seq: _body(f, t_tx if f.tx else t_rx) for f in frames}
    scpi, _ratio = is_scpi_like([texts[f.seq] for f in tx])
    family = ProtocolFamily.SCPI if scpi else ProtocolFamily.GENERIC
    toks: dict[int, Tokens] = {}
    for f in tx:
        t = scpi_tokens(texts[f.seq]) if scpi else None
        toks[f.seq] = t if t is not None else generic_tokens(texts[f.seq])
    arg_counts: dict[str, set[int]] = defaultdict(set)
    for f in tx:
        arg_counts[toks[f.seq].key].add(len(toks[f.seq].args))
    drafts: dict[str, Draft] = {}
    for f in tx:
        t = toks[f.seq]
        suffix = f"/{len(t.args)}" if len(arg_counts[t.key]) > 1 else ""
        key = t.key + suffix
        type_id = "tx_" + _ident(key.replace("?", "_q").replace("*", "").replace("/", "_n")).strip("_")
        f.type_id = type_id
        d = drafts.get(type_id)
        if d is None:
            d = Draft(
                type_id,
                key,
                TX,
                "tokens",
                [],
                header=t.header,
                header_sep=t.header_sep or " ",
                method="tokenize.scpi" if scpi else "tokenize.generic",
            )
            if scpi:
                d.scpi_query = t.is_query
            drafts[type_id] = d
        d.frames.append(f)
        d.tokens.append(t)
    for d in drafts.values():
        seps = {"," if any("," in a for a in t.text.split(t.header, 1)[-1:]) else d.arg_sep for t in d.tokens}
        d.arg_sep = "," if "," in seps or scpi else (" " if any(len(t.args) > 1 for t in d.tokens) else ",")
        heads = Counter(t.header for t in d.tokens)
        d.header = heads.most_common(1)[0][0]

    pairing = pair(frames, lambda fr: loose_signature(texts[fr.seq]))
    responded: set[int] = set()
    for d in list(drafts.values()):
        rate = pairing.rate.get(d.id, 0.0)
        d.answered_rate = rate
        expects = d.scpi_query if d.scpi_query is not None else rate >= 0.5
        if d.scpi_query is not None and not d.scpi_query and rate >= 0.8:
            expects = True
        if d.scpi_query and rate < 0.5:
            ctx.warnings.append(f"{d.key}: query answered in only {rate:.0%} of cases")
        if not expects:
            continue
        resp_frames = [pairing.responses[f.seq] for f in d.frames if f.seq in pairing.responses]
        if not resp_frames:
            continue
        rid = "rx_" + d.id.removeprefix("tx_")
        r = Draft(rid, f"response:{d.key}", RX, "tokens", resp_frames, header="", request=d.id, method="pairing.timing")
        r.answered_rate = rate
        d.response = rid
        d.latencies = pairing.latency_ns.get(d.id, [])
        for rf in resp_frames:
            rf.type_id = rid
            responded.add(rf.seq)
        drafts[rid] = r

    # Unpaired instrument frames are streamed (unsolicited) messages.
    stream_groups_: dict[str, list[Frame]] = defaultdict(list)
    for f in rx:
        if f.seq not in responded:
            sig = loose_signature(texts[f.seq]) if scpi else shape_signature(texts[f.seq])
            stream_groups_[sig].append(f)
    n = 1
    for _sig, group in sorted(stream_groups_.items(), key=lambda kv: -len(kv[1])):
        if len(group) < 2:
            ctx.warnings.append(f"unpaired instrument frame {group[0].raw!r} was not typed")
            continue
        sid = "rx_stream" if n == 1 else f"rx_stream_{n}"
        n += 1
        d = Draft(sid, f"stream:{n - 1}", RX, "tokens", group, unsolicited=True, method="shape.cluster")
        for f in group:
            f.type_id = sid
        drafts[sid] = d

    for d in drafts.values():
        _ascii_fields(ctx, d, texts, scpi)
    return family, framing, None, list(drafts.values()), frames


def _ascii_fields(ctx: _Ctx, d: Draft, texts: dict[int, str], scpi: bool) -> None:
    bodies = [texts[f.seq] for f in d.frames]
    signatures = {shape_signature(b) for b in bodies}
    if not d.tx and not scpi and len(signatures) == 1 and len(bodies) >= 2:
        d.layout = "columns"
        _column_fields(ctx, d, bodies)
        return
    if d.tx:
        token_lists = [list(t.args) for t in d.tokens]
    else:
        heads = {generic_tokens(b).header for b in bodies}
        if not scpi and len(heads) == 1 and next(iter(heads)):
            gt = [generic_tokens(b) for b in bodies]
            d.header = gt[0].header
            d.header_sep = gt[0].header_sep
            d.arg_sep = "," if any("," in b for b in bodies) else " "
            token_lists = [list(generic_tokens(b, d.arg_sep).args) for b in bodies]
        else:
            d.header = ""
            d.arg_sep = ","
            token_lists = [split_args(b.strip(), ",") for b in bodies]
    counts = Counter(len(t) for t in token_lists)
    width = counts.most_common(1)[0][0]
    keep = [i for i, t in enumerate(token_lists) if len(t) == width]
    if len(keep) < len(token_lists):
        ctx.warnings.append(f"{d.key}: {len(token_lists) - len(keep)} frames with a different argument count ignored")
        d.frames = [d.frames[i] for i in keep]
        token_lists = [token_lists[i] for i in keep]
    for i in range(width):
        column = [t[i] for t in token_lists]
        d.fields.append(_ascii_field(ctx, d, i, column, token=i, offset=None, fixed=False, scpi=scpi))


def _ascii_field(
    ctx: _Ctx,
    d: Draft,
    index: int,
    column: list[str],
    *,
    token: int | None,
    offset: int | None,
    fixed: bool,
    scpi: bool,
) -> FieldModel:
    typing = type_tokens(column, tx=d.tx, fixed_width=fixed)
    enc = typing.encoding
    fid = f"f{index}"
    role = FieldRole(typing.role)
    name = "value" if index == 0 else f"arg{index}"
    label = units = ui_element = None
    events = ctx.refs(d.frames)
    alternatives: list[Alternative] = []
    confidence = 0.9
    enum_values: list[EnumValue] = []
    constant: str | None = None
    rng: ValueRange | None = None
    reason = typing.reason
    if role is FieldRole.CONSTANT:
        constant = column[0].strip() if not fixed else column[0]
    elif enc.is_numeric:
        values = [decode(enc, c) for c in column]
        nums = [float(v) for v in values if isinstance(v, int | float)]
        if nums:
            rng = ValueRange(minimum=min(nums), maximum=max(nums))
        hit = correlate_values(ctx.ui, d.frames, values, after=not d.tx)  # type: ignore[arg-type]
        if hit is not None:
            base, units = parse_label(hit.label)
            name, label, ui_element = _ident(base), hit.label, hit.element
            confidence = 0.97
            reason = f"matches UI '{hit.label}' in {hit.support} instances"
            events = events + ctx.ui_refs(hit.events)
        alternatives.append(Alternative(description="ASCII string", score=0.1, reason="numbers parse cleanly"))
    elif role is FieldRole.ENUM:
        mapping = correlate_enum(ctx.ui, d.frames, [c.strip() for c in column]) if d.tx else None
        counts = Counter(c.strip() for c in column)
        if mapping is not None:
            base, units = parse_label(mapping.label)
            name, label, ui_element = _ident(base), mapping.label, mapping.element
            reason = f"one-to-one with UI choices of '{mapping.label}'"
            confidence = 0.97
        for wire, count in sorted(counts.items()):
            ui_value = mapping.mapping.get(wire) if mapping else None
            enum_values.append(EnumValue(wire=wire, label=ui_value or wire, ui_value=ui_value, count=count))
    if (
        scpi
        and d.key.startswith("response:")
        and normalize_header(d.key.split(":", 1)[1]) == "SYST:ERR?"
        and index == 0
    ):
        role = FieldRole.STATUS
        name = "code"
        reason = "SCPI error queue: first element is the error number, 0 means no error"
    return FieldModel(
        id=fid,
        name=name,
        role=role,
        token=token,
        offset=offset,
        length=len(column[0]) if fixed else None,
        encoding=EncodingModel(**enc.to_dict()),
        constant=constant,
        enum=enum_values,
        units=units,
        label=label,
        ui_element=ui_element,
        range=rng,
        status_ok=["0"] if role is FieldRole.STATUS else [],
        evidence=_ev("fields.ascii", confidence, len(column), events=events, alternatives=alternatives, notes=reason),
    )


def _column_fields(ctx: _Ctx, d: Draft, bodies: list[str]) -> None:
    tokens = shape_tokens(bodies[0])
    index = 0
    pending: tuple[int, str] | None = None
    fields: list[FieldModel] = []

    def flush() -> None:
        nonlocal pending, index
        if pending is None:
            return
        col, text = pending
        fields.append(
            FieldModel(
                id=f"f{index}",
                name=f"const_{col}",
                role=FieldRole.CONSTANT,
                offset=col,
                length=len(text),
                constant=text,
                evidence=_ev("fields.columns", 1.0, len(bodies), notes="identical in every frame"),
            )
        )
        index += 1
        pending = None

    for col, _cls, tok in tokens:
        column = [b[col : col + len(tok)] for b in bodies]
        # Words that never varied in a response may vary later (e.g. ST/US); only
        # punctuation and spacing are treated as literal layout.
        literal = ("p", "s", "a") if d.tx else ("p", "s")
        if len(set(column)) == 1 and _cls in literal:
            pending = (pending[0], pending[1] + tok) if pending else (col, tok)
            continue
        flush()
        fld = _ascii_field(ctx, d, index, column, token=None, offset=col, fixed=True, scpi=False)
        if fld.encoding is not None and fld.encoding.kind is EncodingKind.ASCII_ENUM and fld.role is FieldRole.VALUE:
            fld.role = FieldRole.ENUM if 2 <= len(set(column)) <= 12 else FieldRole.VALUE
        fields.append(fld)
        index += 1
    flush()
    used: set[str] = set()
    for f in fields:
        if f.role is FieldRole.ENUM and f.name in ("value", f"arg{f.id[1:]}"):
            f.name = "state"
        f.name = _unique(f.name if f.role is not FieldRole.VALUE or f.name != f"arg{f.id[1:]}" else "value", used)
    d.fields = fields


def _modbus_ascii(
    ctx: _Ctx, framing: FramingModel, frames: list[Frame], t_tx: bytes, t_rx: bytes
) -> tuple[ProtocolFamily, FramingModel, ChecksumModel | None, list[Draft], list[Frame]]:
    """Modbus ASCII: each frame is a fixed-width hex text; typed by function and register."""
    drafts: dict[str, Draft] = {}
    texts = {f.seq: _body(f, t_tx if f.tx else t_rx) for f in frames}
    for f in frames:
        if not f.tx:
            continue
        pdu = bytes.fromhex(texts[f.seq][3:-2])
        key = request_key(pdu)
        tid = "tx_" + key.replace(":", "_")
        f.type_id = tid
        d = drafts.setdefault(tid, Draft(tid, key, TX, "columns", [], method="modbus.ascii"))
        d.frames.append(f)
        d.name = request_name(pdu)
    pairing = pair(frames, lambda fr: texts[fr.seq][3:5] + str(len(texts[fr.seq])))
    for d in list(drafts.values()):
        resp = [pairing.responses[f.seq] for f in d.frames if f.seq in pairing.responses]
        if not resp:
            continue
        rid = "rx_" + d.id.removeprefix("tx_")
        for rf in resp:
            rf.type_id = rid
        d.response = rid
        d.latencies = pairing.latency_ns.get(d.id, [])
        drafts[rid] = Draft(rid, f"response:{d.key}", RX, "columns", resp, request=d.id, method="modbus.ascii")
    for d in drafts.values():
        bodies = [texts[f.seq] for f in d.frames]
        if len({len(b) for b in bodies}) != 1:
            ctx.warnings.append(f"{d.key}: Modbus ASCII frames of different lengths")
            continue
        width = len(bodies[0])
        fields: list[FieldModel] = []
        cols = [(0, 1, "start"), (1, 2, "address"), (3, 2, "function")]
        for off, ln, nm in cols:
            fields.append(
                FieldModel(
                    id=f"f{len(fields)}",
                    name=nm,
                    role=FieldRole.CONSTANT,
                    offset=off,
                    length=ln,
                    constant=bodies[0][off : off + ln],
                    evidence=_ev("modbus.ascii", 1.0, len(bodies)),
                )
            )
        for off in range(5, width - 2, 4):
            ln = min(4, width - 2 - off)
            column = [b[off : off + ln] for b in bodies]
            if len(set(column)) == 1:
                fields.append(
                    FieldModel(
                        id=f"f{len(fields)}",
                        name=f"const_{off}",
                        role=FieldRole.CONSTANT,
                        offset=off,
                        length=ln,
                        constant=column[0],
                        evidence=_ev("modbus.ascii", 1.0, len(bodies)),
                    )
                )
            else:
                fields.append(
                    FieldModel(
                        id=f"f{len(fields)}",
                        name=f"register_{(off - 5) // 4}",
                        role=FieldRole.VALUE,
                        offset=off,
                        length=ln,
                        encoding=EncodingModel(kind=EncodingKind.ASCII_HEX, width=ln, zero_pad=True),
                        evidence=_ev("modbus.ascii", 0.8, len(bodies), notes="16-bit register as hex"),
                    )
                )
        fields.append(
            FieldModel(
                id=f"f{len(fields)}",
                name="lrc",
                role=FieldRole.CHECKSUM,
                offset=width - 2,
                length=2,
                evidence=_ev("modbus.ascii", 1.0, len(bodies), notes="LRC over decoded bytes"),
            )
        )
        d.fields = fields
    checksum = ChecksumModel(
        algorithm="SUM-8/TWOS-COMPLEMENT",
        width=8,
        start=1,
        end=-2,
        position=-2,
        endian="be",
        encoding="ascii_hex",
        covered="hex",
        evidence=_ev(
            "modbus.ascii",
            1.0,
            len(frames),
            events=ctx.refs(frames),
            notes="LRC (two's complement of the byte sum) over the hex-decoded frame",
        ),
    )
    return ProtocolFamily.MODBUS_ASCII, framing, checksum, list(drafts.values()), frames


# ----- binary ----------------------------------------------------------------------------


@dataclass
class _Layout:
    start: bytes = b""
    length_offset: int | None = None
    length_width: int = 0
    checksum: ChecksumSpec | None = None

    def excluded(self, size: int) -> set[int]:
        out = set(range(len(self.start)))
        if self.length_offset is not None:
            out |= set(range(self.length_offset, self.length_offset + self.length_width))
        if self.checksum is not None:
            begin = size + self.checksum.position
            out |= set(range(begin, begin + self.checksum.nbytes))
        return out


def _counter_positions(frames: list[Frame], positions: list[int]) -> set[int]:
    out: set[int] = set()
    by_conn: dict[tuple[int, int], list[Frame]] = defaultdict(list)
    for f in frames:
        by_conn[(f.session, f.connection)].append(f)
    for p in positions:
        steps = good = 0
        for seq in by_conn.values():
            seq.sort(key=lambda f: f.t_ns)
            for a, b in itertools.pairwise(seq):
                if len(a.raw) > p and len(b.raw) > p:
                    steps += 1
                    good += (b.raw[p] - a.raw[p]) % 256 == 1
        if steps >= 4 and good / steps >= 0.9:
            out.add(p)
    return out


def _opcode_position(frames: list[Frame], candidates: list[int]) -> tuple[int | None, list[Alternative]]:
    best: tuple[float, int] | None = None
    alts: list[Alternative] = []
    n = len(frames)
    for p in candidates:
        groups: dict[int, list[int]] = defaultdict(list)
        for f in frames:
            if len(f.raw) > p:
                groups[f.raw[p]].append(len(f.raw))
        if len(groups) < 2 or len(groups) > max(2, min(64, n // 2)):
            continue
        homogeneous = sum(Counter(v).most_common(1)[0][1] for v in groups.values()) / n
        score = homogeneous - 0.002 * len(groups) - 0.001 * p
        alts.append(
            Alternative(
                description=f"opcode at offset {p}",
                score=round(score, 4),
                reason=f"{len(groups)} values, {homogeneous:.0%} length-homogeneous",
            )
        )
        if best is None or score > best[0]:
            best = (score, p)
    alts.sort(key=lambda a: -a.score)
    if best is None or best[0] < 0.8:
        return None, alts
    return best[1], [a for a in alts if a.description != f"opcode at offset {best[1]}"][:3]


def _binary(
    ctx: _Ctx, tx_groups: list[list[Chunk]], rx_groups: list[list[Chunk]], transport: TransportSpec
) -> tuple[ProtocolFamily, FramingModel, ChecksumModel | None, list[Draft], list[Frame]]:
    streams = ctx.streams
    family = ProtocolFamily.GENERIC
    layout = _Layout()
    baud = transport.serial.baudrate if transport.serial else None
    gap = gap_threshold_ns(baud)
    alternatives: list[Alternative] = []
    framing_kind: FramingKind
    length_field: LengthFieldModel | None = None
    frame_length = 0
    method = ""
    conf = 1.0
    notes = ""
    if streams.message_oriented:
        tx = split_messages(tx_groups, TX)
        rx = split_messages(rx_groups, RX)
        framing_kind, method = FramingKind.MESSAGE, "framing.transport"
        notes = f"{streams.transport.value} transfers carry message boundaries"
    else:
        gtx = split_messages(tx_groups, TX, gap)
        grx = split_messages(rx_groups, RX, gap)
        rtu = (
            recognize_rtu([f.raw for f in gtx], [f.raw for f in grx])
            if streams.transport is not Transport.TCP
            else None
        )
        if rtu is not None:
            tx, rx = gtx, grx
            family = ProtocolFamily.MODBUS_RTU
            framing_kind, method = FramingKind.MODBUS_RTU, "modbus.rtu"
            layout.checksum = spec_for("CRC-16/MODBUS", start=0, end=-2, position=-2, endian="le")
            notes = f"{rtu.ratio:.0%} of frames are valid Modbus RTU (unit {rtu.address})"
        else:
            cand, alts = search_length_field([tx_groups, rx_groups])
            fixed = fixed_length([tx_groups, rx_groups]) if cand is None else None
            if cand is not None:
                tx = split_by_length(tx_groups, TX, cand)
                rx = split_by_length(rx_groups, RX, cand)
                framing_kind, method = FramingKind.LENGTH_PREFIXED, "framing.length_field"
                layout.start = cand.start
                layout.length_offset, layout.length_width = cand.offset, cand.width
                header_end = cand.offset + cand.width
                length_field = LengthFieldModel(
                    offset=cand.offset,
                    width=cand.width,
                    endian=cand.endian,
                    adjust=cand.adjust,
                    includes_header=cand.adjust <= 0,
                )
                notes = (
                    f"length field predicts all {cand.frames} frame boundaries; "
                    f"{cand.aligned:.0%} coincide with read/write boundaries; "
                    f"counts {'the whole frame' if cand.adjust <= 0 else f'bytes after a {header_end}-byte header'}"
                )
                alternatives = [
                    Alternative(description=a.describe(), score=a.aligned, reason="also consistent") for a in alts
                ]
                conf = 0.95 if not alts else 0.85
                tcp = recognize_tcp([f.raw for f in tx + rx])
                if tcp is not None:
                    family = ProtocolFamily.MODBUS_TCP
                    # MBAP starts with a transaction id, never a start sequence.
                    layout.start = b""
                    notes += "; Modbus TCP (MBAP) recognized"
            elif fixed is not None:
                tx = split_fixed(tx_groups, TX, fixed)
                rx = split_fixed(rx_groups, RX, fixed)
                framing_kind, method, frame_length = FramingKind.FIXED_LENGTH, "framing.fixed", fixed
                notes = f"every read and write is {fixed} bytes"
                conf = 0.8
            else:
                tx, rx = gtx, grx
                framing_kind, method = FramingKind.MESSAGE, "framing.gap"
                notes = f"frames delimited by silences of {gap / 1e6:.1f} ms"
                conf = 0.6
    frames = order_frames(tx + rx)
    if not frames:
        raise InferenceError("no binary frames found")
    if framing_kind is FramingKind.LENGTH_PREFIXED and not layout.start and family is ProtocolFamily.GENERIC:
        firsts = {f.raw[0] for f in frames}
        if len(firsts) == 1 and len(frames) >= 4:
            layout.start = bytes(firsts)
            notes += f"; every frame starts with {layout.start.hex()}"
    checksum_model: ChecksumModel | None = None
    if layout.checksum is None:
        found = checksum_search.search([f.raw for f in frames])
        if not found and tx:
            found = checksum_search.search([f.raw for f in tx])
        if found:
            best = found[0]
            layout.checksum = best.spec
            alt = [
                Alternative(description=c.describe(), score=1.0 - 0.1 * c.rank, reason="also validates every frame")
                for c in found[1:4]
            ]
            checksum_model = _checksum_model(
                best.spec,
                _ev(
                    "checksum.search",
                    0.98 if best.rank == 0 else 0.9,
                    len(frames),
                    events=ctx.refs(frames),
                    alternatives=alt,
                    notes=f"validates all {best.frames} distinct frames tested",
                ),
            )
    else:
        checksum_model = _checksum_model(
            layout.checksum,
            _ev("modbus.rtu", 1.0, len(frames), events=ctx.refs(frames), notes="Modbus RTU mandates CRC-16/MODBUS"),
        )
    framing = FramingModel(
        kind=framing_kind,
        encoding="binary",
        start=layout.start.hex() if framing_kind is not FramingKind.MESSAGE else "",
        length_field=length_field,
        frame_length=frame_length,
        gap_ms=gap / 1e6 if framing_kind is FramingKind.MESSAGE and not streams.message_oriented else None,
        evidence=_ev(method, conf, len(frames), events=ctx.refs(frames), alternatives=alternatives, notes=notes),
    )
    drafts = _binary_types(ctx, frames, layout, family)
    return family, framing, checksum_model, drafts, frames


def _checksum_model(spec: ChecksumSpec, evidence: Evidence) -> ChecksumModel:
    return ChecksumModel(
        algorithm=spec.algorithm,
        width=spec.width,
        start=spec.start,
        end=spec.end,
        position=spec.position,
        endian=spec.endian,
        encoding=spec.encoding,
        poly=spec.poly,
        init=spec.init,
        refin=spec.refin,
        refout=spec.refout,
        xorout=spec.xorout,
        evidence=evidence,
    )


def _binary_types(ctx: _Ctx, frames: list[Frame], layout: _Layout, family: ProtocolFamily) -> list[Draft]:
    drafts: dict[str, Draft] = {}
    header: dict[Direction, dict[int, str]] = {TX: {}, RX: {}}
    opcode_pos: dict[Direction, int | None] = {}
    for direction in (TX, RX):
        dframes = [f for f in frames if f.direction is direction]
        if not dframes:
            continue
        minlen = min(len(f.raw) for f in dframes)
        excluded = (
            set().union(*(layout.excluded(len(f.raw)) for f in dframes)) if layout.checksum else layout.excluded(minlen)
        )
        positions = [
            p
            for p in range(min(minlen, 8))
            if p not in excluded and p < minlen - (layout.checksum.nbytes if layout.checksum else 0)
        ]
        consts = [p for p in positions if len({f.raw[p] for f in dframes}) == 1]
        counters = _counter_positions(dframes, [p for p in positions if p not in consts])
        for p in consts:
            header[direction][p] = "constant"
        for p in counters:
            header[direction][p] = "counter"
        if family is ProtocolFamily.MODBUS_RTU:
            opcode_pos[direction] = 1
        elif family is ProtocolFamily.MODBUS_TCP:
            opcode_pos[direction] = 7
        else:
            cands = [p for p in positions if p not in consts and p not in counters]
            pos, alts = _opcode_position(dframes, cands)
            opcode_pos[direction] = pos
            if pos is None and len({len(f.raw) for f in dframes}) > 1:
                ctx.warnings.append(f"{direction.value}: no opcode position explains the frame structure")
            del alts
        # Header constants precede the opcode; later constants belong to message layouts.
        limit = opcode_pos[direction]
        limit = 4 if limit is None else limit
        for p in [p for p, role in header[direction].items() if role == "constant" and p > limit]:
            del header[direction][p]
    # Constant in both directions at the same offset: an address.
    for p, role in list(header[TX].items()):
        if role == "constant" and header[RX].get(p) == "constant":
            tv = {f.raw[p] for f in frames if f.tx}
            rv = {f.raw[p] for f in frames if not f.tx}
            if tv == rv and tv.isdisjoint({0x00, 0xFF}):
                header[TX][p] = header[RX][p] = "address"

    def key_of(f: Frame) -> str:
        if family is ProtocolFamily.MODBUS_RTU and f.tx:
            return request_key(f.raw[1:-2])
        if family is ProtocolFamily.MODBUS_TCP and f.tx:
            return request_key(f.raw[7:])
        pos = opcode_pos.get(f.direction)
        base = f"{f.raw[pos]:02x}" if pos is not None and len(f.raw) > pos else "msg"
        return base

    for f in frames:
        if f.tx or family not in (ProtocolFamily.MODBUS_RTU, ProtocolFamily.MODBUS_TCP):
            f.type_id = ("tx_" if f.tx else "rx_") + key_of(f).replace(":", "_")
    for f in frames:
        if f.type_id is None:
            continue
        d = drafts.get(f.type_id)
        if d is None:
            d = Draft(f.type_id, key_of(f), f.direction, "binary", [], method="cluster.opcode")
            drafts[f.type_id] = d
        d.frames.append(f)
    # Split clusters whose members differ in length (structure differs).
    for tid in list(drafts):
        d = drafts[tid]
        lengths = Counter(len(f.raw) for f in d.frames)
        if len(lengths) > 1:
            del drafts[tid]
            notes = _alignment_notes([f.raw for f in d.frames])
            for n, _ in lengths.most_common():
                sub = [f for f in d.frames if len(f.raw) == n]
                nid = f"{tid}_l{n}"
                for f in sub:
                    f.type_id = nid
                drafts[nid] = Draft(
                    nid, f"{d.key}/{n}", d.direction, "binary", sub, method="cluster.opcode+length", notes=notes
                )

    def shape(f: Frame) -> str:
        if family in (ProtocolFamily.MODBUS_RTU, ProtocolFamily.MODBUS_TCP) and not f.tx:
            pos = opcode_pos.get(RX) or 1
            return f"{f.raw[pos]:02x}/{len(f.raw)}"
        return f.type_id or "?"

    pairing = pair(frames, shape)
    responded: set[int] = set()
    for d in [d for d in list(drafts.values()) if d.tx]:
        rate = pairing.rate.get(d.id, 0.0)
        d.answered_rate = rate
        if rate < 0.5:
            if rate > 0:
                ctx.warnings.append(f"{d.key}: answered in only {rate:.0%} of cases; treated as no response")
            continue
        resp = [pairing.responses[f.seq] for f in d.frames if f.seq in pairing.responses]
        d.latencies = pairing.latency_ns.get(d.id, [])
        if family in (ProtocolFamily.MODBUS_RTU, ProtocolFamily.MODBUS_TCP):
            rid = "rx_" + d.id.removeprefix("tx_")
            for rf in resp:
                rf.type_id = rid
            drafts[rid] = Draft(rid, f"response:{d.key}", RX, "binary", resp, request=d.id, method="pairing.timing")
            d.response = rid
        else:
            ids = Counter(rf.type_id for rf in resp)
            rid_any = ids.most_common(1)[0][0]
            if rid_any is not None:
                d.response = rid_any
                drafts[rid_any].request = drafts[rid_any].request or d.id
        responded.update(rf.seq for rf in resp)
    for d in drafts.values():
        if not d.tx and d.request is None:
            d.unsolicited = True
    for d in drafts.values():
        if family in (ProtocolFamily.MODBUS_RTU, ProtocolFamily.MODBUS_TCP):
            _modbus_fields(ctx, d, family)
        else:
            _binary_fields(ctx, d, layout, header[d.direction], opcode_pos.get(d.direction), family)
    # A response field equal to the request's field at the same offset is an echo.
    for d in drafts.values():
        if d.tx or d.request is None:
            continue
        req = drafts[d.request]
        pairs = [(pairing.responses[f.seq], f) for f in req.frames if f.seq in pairing.responses]
        pairs = [(r, q) for r, q in pairs if r.type_id == d.id]
        for fld in d.fields:
            if (
                fld.offset is None
                or fld.length is None
                or fld.role not in (FieldRole.VALUE, FieldRole.CONSTANT, FieldRole.COUNTER)
            ):
                continue
            rq = next((x for x in req.fields if x.offset == fld.offset and x.length == fld.length), None)
            if rq is None or rq.role not in (FieldRole.COUNTER, FieldRole.VALUE, FieldRole.ENUM) or len(pairs) < 2:
                continue
            a, b = fld.offset, fld.offset + fld.length
            same = sum(r.raw[a:b] == q.raw[a:b] for r, q in pairs)
            if same / len(pairs) >= 0.95 and len({q.raw[a:b] for _, q in pairs}) > 1:
                fld.role = FieldRole.ECHO
                fld.echo_of = rq.id
                fld.name = f"{rq.name}_echo"
                fld.encoding = rq.encoding
                fld.constant = None
                fld.enum = list(rq.enum)
                fld.evidence = _ev(
                    "pattern.echo",
                    0.97,
                    len(pairs),
                    events=ctx.refs([q for _, q in pairs]),
                    notes=f"equals request field {rq.name} in {same}/{len(pairs)} exchanges",
                )
    if family not in (ProtocolFamily.MODBUS_RTU, ProtocolFamily.MODBUS_TCP):
        _detect_status(ctx, [d for d in drafts.values() if not d.tx and d.request is not None])
    return list(drafts.values())


def _alignment_notes(raws: list[bytes], limit: int = 30) -> str:
    """Describe a variable-length cluster through progressive multiple sequence alignment.

    Members of one opcode group that differ in length are aligned before the group is
    split by length; the aligned constant and variable spans are kept as evidence of how
    the variants relate (e.g. an optional trailing parameter).
    """
    distinct = list(dict.fromkeys(raws))[:limit]
    alignment = progressive_align(distinct)
    spans = field_spans(alignment)
    parts = [f"{'const' if constant else 'var'}[{start}:{end}]" for start, end, constant in spans]
    return f"split by length; aligned structure of {len(distinct)} variants: " + " ".join(parts)


def _binary_fields(
    ctx: _Ctx,
    d: Draft,
    layout: _Layout,
    header: dict[int, str],
    opcode_pos: int | None,
    family: ProtocolFamily,
) -> None:
    raws = [f.raw for f in d.frames]
    size = len(raws[0])
    d_fields: list[FieldModel] = []
    taken: set[int] = set()

    def add(offset: int, length: int, role: FieldRole, name: str, evidence: Evidence, **kw: Any) -> None:
        d_fields.append(FieldModel(id="", name=name, role=role, offset=offset, length=length, evidence=evidence, **kw))
        taken.update(range(offset, offset + length))

    n = len(raws)
    if layout.start:
        add(0, len(layout.start), FieldRole.START, "start", _ev("framing.start", 1.0, n), constant=layout.start.hex())
    if layout.length_offset is not None:
        add(
            layout.length_offset,
            layout.length_width,
            FieldRole.LENGTH,
            "length",
            _ev("framing.length_field", 1.0, n, notes="computed from the frame size"),
        )
    if layout.checksum is not None:
        begin = size + layout.checksum.position
        add(
            begin,
            layout.checksum.nbytes,
            FieldRole.CHECKSUM,
            "checksum",
            _ev("checksum.search", 1.0, n, notes=layout.checksum.describe()),
        )
    for p, role in sorted(header.items()):
        if p in taken or p >= size:
            continue
        if role == "address":
            add(
                p,
                1,
                FieldRole.ADDRESS,
                "address",
                _ev("pattern.address", 0.9, n, notes="same value at this offset in every frame in both directions"),
                constant=raws[0][p : p + 1].hex(),
            )
        elif role == "constant":
            name = "report_id" if p == 0 and ctx.streams.transport is Transport.HID else f"const_{p}"
            add(p, 1, FieldRole.CONSTANT, name, _ev("pattern.constant", 1.0, n), constant=raws[0][p : p + 1].hex())
        elif role == "counter":
            add(
                p,
                1,
                FieldRole.COUNTER,
                "seq",
                _ev("pattern.counter", 0.95, n, notes="increments by one between consecutive frames"),
                encoding=EncodingModel(kind=EncodingKind.UINT, width=1),
            )
    if opcode_pos is not None and opcode_pos not in taken and opcode_pos < size:
        add(
            opcode_pos,
            1,
            FieldRole.OPCODE,
            "opcode",
            _ev("cluster.opcode", 0.95, n, notes="value partitions frames into structurally homogeneous groups"),
            constant=raws[0][opcode_pos : opcode_pos + 1].hex(),
        )
    # Remaining regions: segment and type.
    regions: list[tuple[int, int]] = []
    start = None
    for i in range(size + 1):
        free = i < size and i not in taken
        if free and start is None:
            start = i
        elif not free and start is not None:
            regions.append((start, i))
            start = None
    after = not d.tx

    def correlate_at(offset: int, width: int) -> Any:
        del offset, width
        if ctx.ui.empty:
            return None
        return correlator(ctx.ui, d.frames, after=after)

    for a, b in regions:
        for seg in segment_region(raws, a, b, correlate_at):
            _add_segment(ctx, d, seg, d_fields, n)
    d_fields.sort(key=lambda f: f.offset or 0)
    used: set[str] = set()
    for i, f in enumerate(d_fields):
        f.id = f"f{i}"
        f.name = _unique(f.name, used)
    d.fields = d_fields


def _modbus_fields(ctx: _Ctx, d: Draft, family: ProtocolFamily) -> None:
    """Modbus PDUs have a fixed grammar: registers are 16-bit big-endian words."""
    raws = [f.raw for f in d.frames]
    size = len(raws[0])
    n = len(raws)
    rtu = family is ProtocolFamily.MODBUS_RTU
    base = 1 if rtu else 7
    func = raws[0][base]
    fields: list[FieldModel] = []

    def const(offset: int, length: int, name: str, role: FieldRole = FieldRole.CONSTANT, note: str = "") -> None:
        fields.append(
            FieldModel(
                id="",
                name=name,
                role=role,
                offset=offset,
                length=length,
                constant=raws[0][offset : offset + length].hex(),
                evidence=_ev("modbus.grammar", 1.0, n, notes=note),
            )
        )

    if rtu:
        const(0, 1, "address", FieldRole.ADDRESS, "unit identifier")
    else:
        fields.append(
            FieldModel(
                id="",
                name="transaction",
                role=FieldRole.COUNTER,
                offset=0,
                length=2,
                encoding=EncodingModel(kind=EncodingKind.UINT, width=2, endian="be"),
                evidence=_ev("modbus.tcp", 0.95, n, notes="MBAP transaction identifier"),
            )
        )
        const(2, 2, "protocol", note="MBAP protocol identifier (0 = Modbus)")
        fields.append(
            FieldModel(
                id="",
                name="length",
                role=FieldRole.LENGTH,
                offset=4,
                length=2,
                evidence=_ev("modbus.tcp", 1.0, n, notes="MBAP length"),
            )
        )
        const(6, 1, "address", FieldRole.ADDRESS, "unit identifier")
    const(base, 1, "function", FieldRole.OPCODE, "Modbus function code")
    end = size - 2 if rtu else size
    words: list[int] = []
    if func & 0x80:
        fields.append(
            FieldModel(
                id="",
                name="exception",
                role=FieldRole.STATUS,
                offset=base + 1,
                length=1,
                encoding=EncodingModel(kind=EncodingKind.UINT, width=1),
                status_ok=[],
                evidence=_ev("modbus.grammar", 1.0, n, notes="exception code"),
            )
        )
    elif d.tx and func in (0x01, 0x02, 0x03, 0x04):
        const(base + 1, 2, "register", note="starting address")
        const(base + 3, 2, "count", note="quantity")
    elif d.tx and func in (0x05, 0x06):
        const(base + 1, 2, "register", note="register address")
        words = [base + 3]
    elif d.tx and func in (0x0F, 0x10):
        const(base + 1, 2, "register", note="starting address")
        const(base + 3, 2, "count", note="quantity")
        const(base + 5, 1, "byte_count", note="byte count")
        words = list(range(base + 6, end, 2))
    elif not d.tx and func in (0x01, 0x02, 0x03, 0x04):
        const(base + 1, 1, "byte_count", note="byte count")
        words = list(range(base + 2, end, 2))
    elif not d.tx:
        const(base + 1, 2, "register", note="register address")
        words = [base + 3] if func in (0x05, 0x06) else []
        if func in (0x0F, 0x10):
            const(base + 3, 2, "count", note="quantity")
    for offset in words:
        fields.append(_modbus_word(ctx, d, raws, offset, len(words) > 1))
    if rtu:
        fields.append(
            FieldModel(
                id="",
                name="checksum",
                role=FieldRole.CHECKSUM,
                offset=size - 2,
                length=2,
                evidence=_ev("modbus.rtu", 1.0, n, notes="CRC-16/MODBUS, little-endian"),
            )
        )
    used: set[str] = set()
    fields.sort(key=lambda f: f.offset or 0)
    for i, f in enumerate(fields):
        f.id = f"f{i}"
        f.name = _unique(f.name, used)
    d.fields = fields


def _modbus_word(ctx: _Ctx, d: Draft, raws: list[bytes], offset: int, many: bool) -> FieldModel:
    spans = [r[offset : offset + 2] for r in raws]
    n = len(raws)
    name = f"register_{offset}" if many else "value"
    if len(set(spans)) == 1 and d.tx:
        return FieldModel(
            id="",
            name=name,
            role=FieldRole.CONSTANT,
            offset=offset,
            length=2,
            constant=spans[0].hex(),
            evidence=_ev("modbus.grammar", 0.9, n, notes="register value never varied"),
        )
    if len(set(spans)) == 1:
        # A register read can return other values later; keep it a value, not a literal.
        value = int.from_bytes(spans[0], "big")
        return FieldModel(
            id="",
            name=name,
            role=FieldRole.VALUE,
            offset=offset,
            length=2,
            encoding=EncodingModel(kind=EncodingKind.UINT, width=2, endian="be"),
            range=ValueRange(minimum=value, maximum=value),
            evidence=_ev("modbus.register", 0.6, n, notes="register value never varied in the recording"),
        )
    corr = None if ctx.ui.empty else correlator(ctx.ui, d.frames, after=not d.tx)
    cands = [
        c
        for c in binary_candidates(spans, corr)
        if c.encoding.kind.value in ("uint", "int") and c.encoding.endian == "be"
    ]
    best = cands[0]
    role = FieldRole.VALUE
    label = units = ui_element = None
    enum_values: list[EnumValue] = []
    events = ctx.refs(d.frames)
    if best.correlation is not None:
        base, units = parse_label(best.correlation.label)
        name, label, ui_element = _ident(base), best.correlation.label, best.correlation.element
        events = events + ctx.ui_refs(best.correlation.events)
    elif d.tx:
        raw_values = [str(int.from_bytes(s, "big")) for s in spans]
        mapping = correlate_enum(ctx.ui, d.frames, raw_values)
        if mapping is not None:
            role = FieldRole.ENUM
            base, units = parse_label(mapping.label)
            name, label, ui_element = _ident(base), mapping.label, mapping.element
            counts = Counter(raw_values)
            enum_values = [
                EnumValue(wire=w, label=mapping.mapping.get(w, w), ui_value=mapping.mapping.get(w), count=c)
                for w, c in sorted(counts.items(), key=lambda kv: int(kv[0]))
            ]
            best = next(c for c in cands if c.encoding.scale_exp == 0)
    nums = [float(v) for v in best.values if isinstance(v, int | float)]
    alts = [
        Alternative(description=a.encoding.describe(), score=round(a.score, 3), reason=a.reason) for a in cands[1:4]
    ]
    return FieldModel(
        id="",
        name=name,
        role=role,
        offset=offset,
        length=2,
        encoding=EncodingModel(**best.encoding.to_dict()),
        enum=enum_values,
        label=label,
        units=units,
        ui_element=ui_element,
        range=ValueRange(minimum=min(nums), maximum=max(nums)) if nums else None,
        evidence=_ev(
            "modbus.register",
            0.97 if label else 0.8,
            n,
            events=events,
            alternatives=alts,
            notes=best.reason + "; Modbus registers are 16-bit big-endian",
        ),
    )


def _add_segment(ctx: _Ctx, d: Draft, seg: Segment, fields: list[FieldModel], n: int) -> None:
    if seg.constant is not None:
        fields.append(
            FieldModel(
                id="",
                name=f"const_{seg.offset}",
                role=FieldRole.CONSTANT,
                offset=seg.offset,
                length=seg.length,
                constant=seg.constant.hex(),
                evidence=_ev("fields.segment", 0.9, n, notes="identical in every instance"),
            )
        )
        return
    cand = seg.candidate
    assert cand is not None
    enc = cand.encoding
    alts = [
        Alternative(description=a.encoding.describe(), score=round(a.score, 3), reason=a.reason)
        for a in seg.alternatives
    ]
    nums = [float(v) for v in cand.values if isinstance(v, int | float)]
    name = f"value_{seg.offset}"
    label = units = ui_element = None
    role = FieldRole.VALUE
    events = ctx.refs(d.frames)
    confidence = min(0.95, 0.5 + cand.score / (seg.length * 2))
    enum_values: list[EnumValue] = []
    if cand.correlation is not None:
        base, units = parse_label(cand.correlation.label)
        name, label, ui_element = _ident(base), cand.correlation.label, cand.correlation.element
        confidence = 0.97
        events = events + ctx.ui_refs(cand.correlation.events)
    if d.tx and enc.kind.value in ("uint", "int") and seg.length in (1, 2) and cand.correlation is None:
        mapping = correlate_enum(ctx.ui, d.frames, [str(int(v)) for v in nums])
        if mapping is not None:
            role = FieldRole.ENUM
            base, units = parse_label(mapping.label)
            name, label, ui_element = _ident(base), mapping.label, mapping.element
            counts = Counter(str(int(v)) for v in nums)
            enum_values = [
                EnumValue(wire=w, label=mapping.mapping.get(w, w), ui_value=mapping.mapping.get(w), count=c)
                for w, c in sorted(counts.items(), key=lambda kv: int(kv[0]))
            ]
            confidence = 0.97
            cand.reason = f"one-to-one with UI choices of '{mapping.label}'"
    fields.append(
        FieldModel(
            id="",
            name=name,
            role=role,
            offset=seg.offset,
            length=seg.length,
            encoding=EncodingModel(**enc.to_dict()),
            enum=enum_values,
            label=label,
            units=units,
            ui_element=ui_element,
            range=ValueRange(minimum=min(nums), maximum=max(nums)) if nums else None,
            evidence=_ev("fields.encoding_search", confidence, n, events=events, alternatives=alts, notes=cand.reason),
        )
    )


_HEADER_ROLES = (
    FieldRole.START,
    FieldRole.ADDRESS,
    FieldRole.OPCODE,
    FieldRole.LENGTH,
    FieldRole.COUNTER,
    FieldRole.ECHO,
)


def _payload_start(d: Draft) -> int | None:
    ends = [
        f.offset + f.length
        for f in d.fields
        if f.offset is not None and f.length is not None and f.role in _HEADER_ROLES
    ]
    return max(ends) if ends else None


def _detect_status(ctx: _Ctx, responses: list[Draft]) -> None:
    """The first payload byte of response types is a status code when several
    response types share that offset and one value dominates it; the dominant
    value is taken as success."""
    by_offset: dict[int, list[Draft]] = defaultdict(list)
    for d in responses:
        start = _payload_start(d)
        if start is not None and start < len(d.frames[0].raw):
            by_offset[start].append(d)
    for offset, items in by_offset.items():
        if len(items) < 2:
            continue
        values: Counter[int] = Counter(fr.raw[offset] for d in items for fr in d.frames)
        dominant, count = values.most_common(1)[0]
        total = sum(values.values())
        if count / total < 0.7 or len(values) > 8:
            continue
        for d in items:
            _split_field_at(d, offset)
            f = next(x for x in d.fields if x.offset == offset)
            if f.length != 1 or f.role not in (FieldRole.VALUE, FieldRole.CONSTANT):
                continue
            f.role = FieldRole.STATUS
            f.constant = None
            f.encoding = EncodingModel(kind=EncodingKind.UINT, width=1)
            f.status_ok = [str(dominant)]
            f.name = _unique("status", {x.name for x in d.fields if x is not f})
            f.evidence = _ev(
                "pattern.status",
                0.85,
                total,
                events=ctx.refs(d.frames),
                notes=f"first payload byte of {len(items)} response types; {dominant} in {count / total:.0%} of frames",
            )


def _split_field_at(d: Draft, offset: int) -> None:
    """Split a constant run so ``offset`` starts a one-byte field of its own."""
    for i, f in enumerate(d.fields):
        if f.role is not FieldRole.CONSTANT or f.offset is None or f.length is None or f.constant is None:
            continue
        if not f.offset <= offset < f.offset + f.length or f.length == 1:
            continue
        data = bytes.fromhex(f.constant)
        parts: list[tuple[int, bytes]] = []
        rel = offset - f.offset
        if rel:
            parts.append((f.offset, data[:rel]))
        parts.append((offset, data[rel : rel + 1]))
        if rel + 1 < len(data):
            parts.append((offset + 1, data[rel + 1 :]))
        new = [
            f.model_copy(update={"offset": o, "length": len(b), "constant": b.hex(), "name": f"const_{o}"})
            for o, b in parts
        ]
        d.fields[i : i + 1] = new
        break
    d.fields.sort(key=lambda x: x.offset or 0)
    used: set[str] = set()
    for i, f in enumerate(d.fields):
        f.id = f"f{i}"
        f.name = _unique(f.name, used)


# ----- shared post-processing ----------------------------------------------------------


def _reflects(ctx: _Ctx, drafts: dict[str, Draft], frames: list[Frame], family: ProtocolFamily) -> None:
    """Response value fields that report the last value set by a command."""
    del ctx
    sets: list[tuple[Draft, FieldModel]] = [
        (d, f) for d in drafts.values() if d.tx for f in d.fields if f.role in (FieldRole.VALUE, FieldRole.ENUM)
    ]
    if not sets:
        return
    ordered = sorted(frames, key=lambda f: (f.session, f.t_ns))
    for d in drafts.values():
        if d.tx or d.request is None:
            continue
        for rf in d.fields:
            if rf.role not in (FieldRole.VALUE, FieldRole.ENUM) or rf.encoding is None:
                continue
            for sd, sf in sets:
                if sd.id == d.request or sf.encoding is None:
                    continue
                if family is ProtocolFamily.SCPI:
                    q = drafts[d.request].key
                    if normalize_header(q.removesuffix("?")) == normalize_header(sd.key) and rf.token == sf.token:
                        rf.reflects = (sd.id, sf.id)
                        rf.name = sf.name
                        rf.units, rf.label = sf.units or rf.units, sf.label or rf.label
                        break
                support = hits = 0
                last: dict[int, Any] = {}
                seen_values: set[str] = set()
                for f in ordered:
                    if f.type_id == sd.id:
                        last[f.session] = _decode_field(sd, sf, f)
                    elif f.type_id == d.id and f.session in last and last[f.session] is not None:
                        value = _decode_field(d, rf, f)
                        support += 1
                        seen_values.add(str(last[f.session]))
                        hits += (value is not None and str(value) == str(last[f.session])) or (
                            isinstance(value, float)
                            and isinstance(last[f.session], int | float)
                            and abs(value - float(last[f.session])) < 1e-6 * max(1.0, abs(value))
                        )
                if support >= 2 and len(seen_values) >= 2 and hits / support >= 0.9:
                    rf.reflects = (sd.id, sf.id)
                    if rf.name.startswith(("value", "arg")):
                        rf.name = sf.name
                    rf.units = rf.units or sf.units
                    rf.label = rf.label or sf.label
                    break


def _decode_field(d: Draft, f: FieldModel, frame: Frame) -> Any:
    if f.encoding is None:
        return None
    enc = Encoding.from_dict(f.encoding.model_dump(mode="json"))
    try:
        if d.layout == "binary" and f.offset is not None and f.length is not None:
            return decode(enc, frame.raw[f.offset : f.offset + f.length])
        if d.layout == "tokens" and f.token is not None:
            idx = d.frames.index(frame) if d.tokens else -1
            if d.tokens and idx >= 0:
                return decode(enc, d.tokens[idx].args[f.token])
            text = frame.raw.decode("latin-1").strip()
            args = split_args(text.split(d.header_sep, 1)[1] if d.header and d.header_sep in text else text, d.arg_sep)
            return decode(enc, args[f.token])
        if d.layout == "columns" and f.offset is not None and f.length is not None:
            return decode(enc, frame.raw.decode("latin-1")[f.offset : f.offset + f.length].strip())
    except (ValueError, IndexError):
        return None
    return None


def _bindings(ctx: _Ctx, drafts: dict[str, Draft], frames: list[Frame]) -> None:
    by_seq = {f.seq: f for f in frames}
    for b in invoke_bindings(ctx.ui, frames, ctx.sessions):
        d = drafts.get(b.type_id)
        if d is None:
            continue
        constants: dict[str, str] = {}
        bound = [by_seq[s] for s in b.instances if s in by_seq]
        for f in d.fields:
            if f.role not in (FieldRole.VALUE, FieldRole.ENUM):
                continue
            vals = {str(_decode_field(d, f, fr)) for fr in bound}
            if len(vals) == 1:
                constants[f.id] = vals.pop()
        d.bindings.append(
            UiBinding(
                element=b.element,
                element_name=b.name,
                kind="invoke",
                constants=constants,
                support=b.support,
                confidence=min(1.0, b.support / max(b.clicks, 1)),
            )
        )
    for d in drafts.values():
        for f in d.fields:
            if f.ui_element and d.tx:
                d.bindings.append(
                    UiBinding(
                        element=f.ui_element,
                        element_name=ctx.ui.names.get(f.ui_element, ""),
                        kind="value",
                        constants={},
                        support=len(d.frames),
                        confidence=0.95,
                    )
                )


def _name(ctx: _Ctx, d: Draft, drafts: dict[str, Draft], family: ProtocolFamily) -> str:
    if d.name:
        return _ident(d.name)
    if not d.tx:
        if d.request is not None and d.request in drafts:
            return f"{_name(ctx, drafts[d.request], drafts, family)}_response"
        return "stream" if d.id == "rx_stream" else _ident(d.id.removeprefix("rx_"))
    invokes = [b for b in d.bindings if b.kind == "invoke"]
    if family is ProtocolFamily.SCPI or (d.layout == "tokens" and d.header):
        base = _ident(d.header.replace("*", "").replace("?", ""))
        if d.header.endswith("?"):
            return f"{base}_query"
        if len(invokes) == 1 and not d.has_inputs() and family is not ProtocolFamily.SCPI:
            return _ident(invokes[0].element_name)
        return base
    labelled = [f for f in d.fields if f.label and f.role in (FieldRole.VALUE, FieldRole.ENUM)]
    modbus = family in (ProtocolFamily.MODBUS_RTU, ProtocolFamily.MODBUS_TCP)
    if len(invokes) == 1 and not invokes[0].constants and not d.has_inputs() and not modbus:
        bound_share = invokes[0].support / max(len(d.frames), 1)
        if bound_share >= 0.6:
            return _ident(invokes[0].element_name)
    if labelled:
        return f"set_{labelled[0].name}"
    if modbus:
        raw = d.frames[0].raw
        pdu = raw[1:-2] if family is ProtocolFamily.MODBUS_RTU else raw[7:]
        if d.response and d.response in drafts:
            resp_labels = [f for f in drafts[d.response].fields if f.label]
            if resp_labels:
                return f"read_{resp_labels[0].name}"
        return request_name(pdu)
    if d.response and d.response in drafts and not d.has_inputs():
        resp_labels = [f for f in drafts[d.response].fields if f.label]
        if resp_labels:
            return f"get_{resp_labels[0].name}"
        return f"query_{d.key.replace('/', '_')}"
    return f"op_{d.key.replace('/', '_')}"


def _carries_data(d: Draft) -> bool:
    """A response carries data when a value field varies or is numeric; a response
    with only status, echo and constant content is an acknowledgement."""
    for f in d.fields:
        if f.role not in (FieldRole.VALUE, FieldRole.ENUM) or f.encoding is None:
            continue
        if f.encoding.kind not in (EncodingKind.ASCII_ENUM, EncodingKind.ASCII_STRING):
            return True
        values = {str(_decode_field(d, f, fr)) for fr in d.frames}
        if len(values) > 1:
            return True
    return False


def _manual(model: ProtocolModel, path: str) -> None:
    entries, pages, digest = manual_assist.extract(path)
    model.manual = ManualInfo(path=path, sha256=digest, pages=pages, commands_found=len(entries))
    for m in model.messages:
        if m.direction is not Direction.TX:
            continue
        entry = manual_assist.match(entries, m.key.split("/")[0])
        if entry is None:
            continue
        m.manual = ManualRef(mnemonic=entry.mnemonic, description=entry.description, page=entry.page, score=1.0)
        if not m.doc:
            m.doc = entry.description


def infer(sessions: Sequence[Session], options: InferOptions | None = None) -> ProtocolModel:
    """Infer a protocol model from one or more sessions of the same instrument."""
    opts = options or InferOptions()
    if not sessions:
        raise InferenceError("no sessions given")
    streams = collect(list(sessions), opts.stream)
    ctx = _Ctx(sessions, streams)
    transport = detect_transport(streams, sessions)
    tx_groups, rx_groups = stream_groups(streams)
    joined = b"".join(c.data for c in streams.chunks)
    if is_ascii(joined) and not streams.message_oriented:
        family, framing, checksum, draft_list, frames = _ascii(ctx, tx_groups, rx_groups)
    else:
        if printable_ratio(joined) > 0.8:
            ctx.warnings.append("stream is mostly printable but treated as binary")
        family, framing, checksum, draft_list, frames = _binary(ctx, tx_groups, rx_groups, transport)
    drafts = {d.id: d for d in draft_list if d.frames and d.fields is not None}
    _reflects(ctx, drafts, frames, family)
    _bindings(ctx, drafts, frames)

    # Names, kinds and classification.
    used: set[str] = set()
    for d in sorted(drafts.values(), key=lambda d: (not d.tx, d.id)):
        d.name = _unique(_name(ctx, d, drafts, family), used)
    messages: dict[str, MessageType] = {}
    for d in drafts.values():
        if d.scpi_query is not None:
            is_query = d.scpi_query
        elif family in (ProtocolFamily.MODBUS_RTU, ProtocolFamily.MODBUS_TCP, ProtocolFamily.MODBUS_ASCII) and d.tx:
            is_query = d.key[:2] in ("01", "02", "03", "04")
        else:
            resp = drafts.get(d.response) if d.response else None
            is_query = resp is not None and not d.has_inputs() and _carries_data(resp)
        if d.tx:
            kind = MessageKind.QUERY if is_query else MessageKind.COMMAND
        else:
            kind = MessageKind.UNSOLICITED if d.unsolicited else MessageKind.RESPONSE
        texts = [d.name, d.key, d.header]
        if d.header in IEEE488_COMMON:
            texts.append(IEEE488_COMMON[d.header])
        ui_texts = [b.element_name for b in d.bindings if b.kind == "invoke"]
        cls, reason = classify(is_query=is_query, has_inputs=d.has_inputs(), texts=texts, ui_texts=ui_texts)
        if not d.tx:
            cls, reason = "read_only", "instrument-to-host message"
        latency = max(d.latencies) / 1e6 if d.latencies else 0.0
        timeout = int(min(5000, max(250, latency * 4)))
        examples: list[Example] = []
        seen: set[bytes] = set()
        for f in d.frames:
            if f.raw in seen:
                continue
            seen.add(f.raw)
            text = f.raw.decode("latin-1") if framing.encoding == "ascii" else None
            examples.append(Example(session=ctx.session_id(f), index=f.events[0], raw=f.raw.hex(), text=text))
            if len(examples) >= MAX_EXAMPLES:
                break
        doc = IEEE488_COMMON.get(d.header, "")
        messages[d.id] = MessageType(
            id=d.id,
            name=d.name,
            direction=d.direction,
            kind=kind,
            key=d.key,
            layout=d.layout,
            header=d.header,
            header_sep=d.header_sep,
            arg_sep=d.arg_sep,
            length=len(d.frames[0].raw) if d.layout == "binary" else None,
            fields=d.fields,
            response=d.response,
            response_timeout_ms=timeout,
            classification=Classification(cls),
            classification_reason=reason,
            ui_bindings=d.bindings,
            examples=examples,
            count=len(d.frames),
            doc=doc,
            evidence=_ev(
                d.method or "cluster",
                d.confidence if d.frames else 0.0,
                len(d.frames),
                events=ctx.refs(d.frames),
                notes=d.notes or (f"answered in {d.answered_rate:.0%} of cases" if d.tx and d.response else ""),
            ),
        )

    # Sequences for the state machine and preconditions (requests only).
    sequences: list[list[str]] = []
    for si in range(len(sessions)):
        seq = [
            f.type_id
            for f in sorted(frames, key=lambda f: f.t_ns)
            if f.session == si and f.tx and f.type_id in messages
        ]
        if seq:
            sequences.append([s for s in seq if s is not None])
    state_changing = {m.id for m in messages.values() if m.direction is TX and m.kind is MessageKind.COMMAND}
    for hit in preconditions(sequences, requirements=state_changing):
        target = messages.get(hit.message)
        if target is None:
            continue
        target.preconditions.append(
            Precondition(
                message=hit.requires,
                disabled_by=list(hit.disabled_by),
                evidence=_ev(
                    "statemachine.precondition",
                    min(0.95, 0.5 + 0.05 * hit.support),
                    hit.support,
                    notes=(
                        f"{messages[hit.requires].name} preceded every one of {hit.support} occurrences "
                        f"in {hit.sessions} session(s)"
                        + (
                            f"; disabled by {', '.join(messages[x].name for x in hit.disabled_by)}"
                            if hit.disabled_by
                            else ""
                        )
                    ),
                ),
            )
        )
    automaton = k_tails(sequences, opts.k) if sequences else None
    state_machine = None
    if automaton is not None:
        state_machine = StateMachineModel(
            k=automaton.k,
            initial=automaton.initial,
            states=[StateModel(id=s, label="accept" if s in automaton.accepting else "") for s in automaton.states],
            transitions=[
                TransitionModel(source=s, target=t, message=m, count=c)
                for (s, t, m), c in sorted(automaton.transitions.items())
            ],
            evidence=_ev(
                "statemachine.ktails",
                0.8,
                sum(len(s) for s in sequences),
                notes=f"k-tails with k={automaton.k} over {len(sequences)} session(s)",
            ),
        )

    exchanges: list[Exchange] = []
    responses_by_req: dict[int, Frame] = {}
    by_conn: dict[tuple[int, int], list[Frame]] = defaultdict(list)
    for f in frames:
        by_conn[(f.session, f.connection)].append(f)
    for conn_frames in by_conn.values():
        conn_frames.sort(key=lambda f: (f.t_ns, f.seq))
        for i, f in enumerate(conn_frames):
            if not f.tx or f.type_id not in messages:
                continue
            want = messages[f.type_id].response
            if want is None:
                continue
            for g in conn_frames[i + 1 :]:
                if g.tx:
                    break
                if g.type_id == want:
                    responses_by_req[f.seq] = g
                    break
    for f in sorted(frames, key=lambda f: (f.session, f.t_ns)):
        if not f.tx or f.type_id not in messages or len(exchanges) >= MAX_EXCHANGES:
            continue
        r = responses_by_req.get(f.seq)
        exchanges.append(
            Exchange(
                session=ctx.session_id(f),
                index=f.events[0],
                request=f.raw.hex(),
                responses=[r.raw.hex()] if r else [],
                message=f.type_id,
                response_message=r.type_id if r else None,
                latency_ms=(r.t_ns - f.t_ns) / 1e6 if r else 0.0,
            )
        )
    unsolicited = [
        f.raw.hex() for f in frames if f.type_id in messages and messages[f.type_id].kind is MessageKind.UNSOLICITED
    ][:500]
    session_refs = []
    for i, s in enumerate(sessions):
        session_refs.append(
            SessionRef(
                id=s.meta.session_id,
                path=opts.session_paths[i] if i < len(opts.session_paths) else "",
                sha256=opts.session_digests[i] if i < len(opts.session_digests) else "",
                events=len(s.events),
                ui_events=len(s.ui_events),
                source=s.meta.source.value,
            )
        )
    model = ProtocolModel(
        name=opts.name or _ident(streams.stream.split(":")[-1].split("\\")[-1].split("/")[-1]) or "instrument",
        calder_version=__version__,
        family=family,
        stream=streams.stream,
        sessions=session_refs,
        transport=transport,
        framing=framing,
        checksum=checksum,
        messages=sorted(messages.values(), key=lambda m: (m.direction is not TX, m.id)),
        state_machine=state_machine,
        exchanges=exchanges,
        unsolicited=unsolicited,
        warnings=ctx.warnings,
    )
    if opts.manual:
        _manual(model, opts.manual)
    _self_check(model, drafts)
    validate_model(model)
    return model


def _self_check(model: ProtocolModel, drafts: dict[str, Draft]) -> None:
    """Decode every observed frame with the runtime codec built from the model.

    Frames the codec cannot decode, or cannot re-encode byte for byte, lower the
    message's confidence and produce a warning, so the review UI shows exactly
    where the generated driver would disagree with the recording.
    """
    try:
        codec = Codec(ProtocolSpec.from_dict(to_runtime(model)))
    except (CodecError, ValueError, KeyError) as exc:
        model.warnings.append(f"runtime codec could not be built: {exc}")
        return
    for m in model.messages:
        spec = codec.spec.message(m.id)
        d = drafts[m.id]
        failures = mismatches = 0
        for f in d.frames:
            try:
                decoded = codec.decode_as(spec, f.raw)
            except (CodecError, ValueError) as exc:
                failures += 1
                if failures == 1:
                    model.warnings.append(f"{m.name}: frame {f.raw!r} does not decode ({exc})")
                continue
            if m.direction is TX and not any(x.role.value == "counter" for x in m.fields):
                try:
                    again = codec.encode(spec, decoded.values)
                except (CodecError, ValueError):
                    again = b""
                if again != f.raw:
                    mismatches += 1
                    if mismatches == 1:
                        model.warnings.append(f"{m.name}: re-encoding {f.raw!r} gives {again!r}")
        for ex in m.examples:
            try:
                ex.values = {
                    k: (v.hex() if isinstance(v, bytes) else v)
                    for k, v in codec.decode_as(spec, bytes.fromhex(ex.raw)).values.items()
                }
            except (CodecError, ValueError):
                ex.values = {}
        total = len(d.frames)
        if failures or mismatches:
            ok = (total - failures - mismatches) / total
            m.evidence.confidence = round(min(m.evidence.confidence, ok * 0.8), 3)
            m.evidence.contradictions = failures + mismatches
