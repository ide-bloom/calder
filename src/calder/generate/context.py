"""Turn a protocol model into the precomputed names, types and texts templates need.

All decisions about identifiers, Python types, literals and docstrings live
here so the templates stay simple loops and the output is deterministic.
"""

from __future__ import annotations

import keyword
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

from calder.infer.correlate import snake
from calder.model.schema import (
    Classification,
    EncodingKind,
    FieldModel,
    FieldRole,
    MessageType,
    ProtocolModel,
    Status,
)
from calder.session.models import Direction, Transport

RESERVED = {"self", "cls", "timeout", "confirm", "values", "driver", "metadata"}
DATA_ROLES = (FieldRole.VALUE, FieldRole.ENUM)
RESPONSE_ROLES = (FieldRole.VALUE, FieldRole.ENUM, FieldRole.STATUS)


def camel(text: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", text)
    out = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not out or not out[0].isalpha():
        out = "X" + out
    return out


def ident(text: str) -> str:
    name = snake(text)
    if keyword.iskeyword(name) or name in RESERVED:
        name += "_"
    return name


def member(label: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").upper() or "VALUE"
    if base[0].isdigit():
        base = f"V_{base}"
    if keyword.iskeyword(base.lower()) and base.lower() == base:
        base += "_"
    name = base
    n = 2
    while name in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name)
    return name


def doc_block(text: str, indent: int) -> str:
    """Wrap text for a docstring body at ``indent`` spaces, max 96 columns."""
    pad = " " * indent
    lines: list[str] = []
    for para in text.strip().split("\n"):
        if not para.strip():
            lines.append("")
            continue
        lines.extend(
            textwrap.wrap(
                para.replace('"""', "'''").replace("\\", "\\\\"),
                width=96 - indent,
                break_long_words=True,
                break_on_hyphens=False,
            )
            or [""]
        )
    return "\n".join((pad + ln) if ln else "" for ln in lines)


@dataclass
class EnumCtx:
    name: str
    base: str  # "StrEnum" or "IntEnum"
    members: list[tuple[str, str, str]]  # (member, literal, label)
    doc: str


@dataclass
class FieldCtx:
    id: str
    key: str  # field name in the runtime protocol (decoded value dict key)
    name: str
    py_type: str
    role: str
    enum: EnumCtx | None
    units: str | None
    label: str | None
    range_text: str
    doc: str
    minimum: float | None
    maximum: float | None


@dataclass
class MessageCtx:
    id: str
    name: str
    class_name: str
    method: str
    kind: str
    classification: str
    direction: str
    inputs: list[FieldCtx]
    outputs: list[FieldCtx]
    response: MessageCtx | None
    return_type: str
    return_kind: str  # none | value | dataclass
    doc: str
    wire: str
    preconditions: list[str]
    example_args: str
    example_ok: bool
    example_json: str
    actuating: bool
    read_only: bool
    sila_id: str
    sila_doc: str
    check_status: bool = True


@dataclass
class AliasCtx:
    name: str
    params: str
    call_args: list[tuple[str, str]]  # (method, args)
    doc: str
    actuating: bool
    returns: str
    return_expr: str


@dataclass
class StreamCtx:
    name: str
    message: MessageCtx


@dataclass
class GenContext:
    model: ProtocolModel
    package: str
    dist_name: str
    class_name: str
    title: str
    feature_id: str
    transport_kind: str
    default_resource: str
    message_mode: bool
    dependencies: list[str]
    enums: list[EnumCtx] = field(default_factory=list)
    messages: list[MessageCtx] = field(default_factory=list)
    responses: list[MessageCtx] = field(default_factory=list)
    aliases: list[AliasCtx] = field(default_factory=list)
    streams: list[StreamCtx] = field(default_factory=list)
    has_preconditions: bool = False
    precondition_example: tuple[str, str, str] | None = None
    first_query: MessageCtx | None = None
    first_actuating: MessageCtx | None = None
    calder_version: str = ""
    warnings: list[str] = field(default_factory=list)


def _py_type(f: FieldModel) -> str:
    enc = f.encoding
    if enc is None:
        return "bytes"
    k = enc.kind
    if k in (EncodingKind.FLOAT, EncodingKind.ASCII_FLOAT):
        return "float"
    if k in (
        EncodingKind.UINT,
        EncodingKind.INT,
        EncodingKind.BCD,
        EncodingKind.BITS,
        EncodingKind.ASCII_INT,
        EncodingKind.ASCII_HEX,
    ):
        return "float" if enc.scale_exp < 0 else "int"
    if k is EncodingKind.BYTES:
        return "bytes"
    return "str"


def _literal(value: Any, fc: FieldCtx) -> str:
    if fc.enum is not None:
        for mem, lit, _label in fc.enum.members:
            if lit == repr(value) or lit == repr(str(value)) or (fc.enum.base == "IntEnum" and lit == str(value)):
                return f"{fc.enum.name}.{mem}"
        return f"{fc.enum.name}({value!r})"
    if fc.py_type == "float":
        return repr(float(value))
    if fc.py_type == "int":
        return repr(int(value))
    if fc.py_type == "bytes":
        return f"bytes.fromhex({value!r})"
    return repr(str(value))


class _Builder:
    def __init__(self, model: ProtocolModel, package: str) -> None:
        self.model = model
        self.package = package
        self.enum_names: set[str] = set()
        self.enums: list[EnumCtx] = []
        self.class_names: set[str] = set()

    def enum_for(self, m: MessageType, f: FieldModel) -> EnumCtx | None:
        if f.role is not FieldRole.ENUM or not f.enum:
            return None
        name = camel(f"{m.name}_{f.name}")
        n = 2
        base_name = name
        while name in self.enum_names:
            name = f"{base_name}{n}"
            n += 1
        self.enum_names.add(name)
        numeric = f.encoding is not None and f.encoding.kind.value in ("uint", "int", "bcd", "bits")
        used: set[str] = set()
        members = []
        for e in f.enum:
            mem = member(e.label or e.wire, used)
            lit = str(int(e.wire)) if numeric else repr(e.wire)
            members.append((mem, lit, e.label))
        doc = f"Values of {m.name}.{f.name}" + (f" (UI: {f.label})" if f.label else "") + "."
        ctx = EnumCtx(name, "IntEnum" if numeric else "StrEnum", members, doc)
        self.enums.append(ctx)
        return ctx

    def field_ctx(self, m: MessageType, f: FieldModel) -> FieldCtx:
        enum = self.enum_for(m, f)
        py_type = enum.name if enum else _py_type(f)
        if f.role is FieldRole.STATUS:
            py_type = (
                "str"
                if f.encoding
                and not f.encoding.kind.value.startswith(("uint", "int"))
                and f.encoding.kind is not EncodingKind.ASCII_INT
                else "int"
            )
        rng = ""
        if f.range is not None:
            rng = f"observed {f.range.minimum:g} to {f.range.maximum:g}"
        parts = []
        if f.label:
            parts.append(f"UI '{f.label}'")
        if f.units:
            parts.append(f"units {f.units}")
        if f.encoding is not None:
            from calder.runtime.encodings import Encoding

            parts.append(Encoding.from_dict(f.encoding.model_dump(mode="json")).describe())
        if rng:
            parts.append(rng)
        ev = f.evidence
        parts.append(f"confidence {ev.confidence:.2f}" + (" (uncertain)" if ev.uncertain else ""))
        return FieldCtx(
            id=f.id,
            key=f.name,
            name=ident(f.name),
            py_type=py_type,
            role=f.role.value,
            enum=enum,
            units=f.units,
            label=f.label,
            range_text=rng,
            doc="; ".join(parts),
            minimum=f.range.minimum if f.range else None,
            maximum=f.range.maximum if f.range else None,
        )

    def class_name(self, name: str) -> str:
        base = camel(name)
        out = base
        n = 2
        while out in self.class_names:
            out = f"{base}{n}"
            n += 1
        self.class_names.add(out)
        return out


def _wire(m: MessageType, model: ProtocolModel) -> str:
    if m.layout == "binary":
        ex = m.examples[0].raw if m.examples else ""
        return f"binary frame, {m.length} bytes, key {m.key}" + (f", e.g. {ex}" if ex else "")
    if m.examples and m.examples[0].text is not None:
        return f"ASCII, e.g. {m.examples[0].text.strip()!r}"
    del model
    return f"ASCII, header {m.header!r}"


def build_context(model: ProtocolModel, *, name: str | None = None) -> GenContext:
    base_name = ident(name or model.name).strip("_") or "instrument"
    package = f"liberated_{base_name}"
    b = _Builder(model, package)
    transport = model.transport.kind
    default_resource = model.transport.resource or ""
    deps = []
    if transport is Transport.SERIAL:
        deps.append("pyserial>=3.5")
    elif transport is Transport.VISA:
        deps.append("pyvisa>=1.14")
    elif transport is Transport.WINUSB:
        deps.append("pyusb>=1.2")
    elif transport is Transport.HID:
        deps.append("hidapi>=0.14")
    active = model.active_messages()
    by_id = {m.id: m for m in active}
    responses: dict[str, MessageCtx] = {}
    for m in active:
        if m.direction is Direction.RX:
            outputs = [b.field_ctx(m, f) for f in m.fields if f.role in RESPONSE_ROLES]
            rctx = MessageCtx(
                id=m.id,
                name=m.name,
                class_name=b.class_name(m.name),
                method=ident(m.name),
                kind=m.kind.value,
                classification="read_only",
                direction="rx",
                inputs=[],
                outputs=outputs,
                response=None,
                return_type="None",
                return_kind="none",
                doc=m.doc,
                wire=_wire(m, model),
                preconditions=[],
                example_args="",
                example_ok=True,
                example_json="{}",
                actuating=False,
                read_only=True,
                sila_id=camel(m.name),
                sila_doc="",
            )
            responses[m.id] = rctx
    messages: list[MessageCtx] = []
    used_methods: set[str] = {"close", "connect", "simulated", "send", "client"}
    for m in active:
        if m.direction is not Direction.TX:
            continue
        inputs = [b.field_ctx(m, f) for f in m.fields if f.role in DATA_ROLES]
        resp = responses.get(m.response) if m.response else None
        data_out = [o for o in resp.outputs if o.role != "status"] if resp else []
        status_out = [o for o in resp.outputs if o.role == "status"] if resp else []
        check_status = True
        if data_out and status_out:
            # The caller sees the status, so the driver reports it instead of raising.
            data_out = status_out + data_out
            check_status = False
        if resp is None or not data_out:
            return_type, return_kind = "None", "none"
        elif len(data_out) == 1:
            return_type, return_kind = data_out[0].py_type, "value"
        else:
            return_type, return_kind = resp.class_name, "dataclass"
        method = ident(m.name)
        while method in used_methods:
            method += "_"
        used_methods.add(method)
        ev = m.evidence
        pre_names = [
            by_id[p.message].name for p in m.preconditions if p.status is not Status.REJECTED and p.message in by_id
        ]
        doc_lines = [m.doc or m.suggested_doc or f"Send {m.key}."]
        doc_lines.append("")
        doc_lines.append(f"Wire: {_wire(m, model)}.")
        doc_lines.append(
            f"Classification: {m.classification.value} ({m.classification_reason}"
            f"{'; confirmed' if m.classification_confirmed else ''})."
        )
        doc_lines.append(
            f"Evidence: {m.count} recorded frames, method {ev.method}, confidence {ev.confidence:.2f}"
            + (" (uncertain)" if ev.uncertain else "")
            + "."
        )
        ui = [x.element_name or x.element for x in m.ui_bindings if x.kind == "invoke"]
        if ui:
            doc_lines.append(f"UI: sent when the operator used {', '.join(sorted(set(ui)))}.")
        if pre_names:
            doc_lines.append(f"Preconditions (hypothesis from recordings): {', '.join(pre_names)} first.")
        if m.manual:
            doc_lines.append(f"Manual: {m.manual.mnemonic}: {m.manual.description}")
        if inputs:
            doc_lines.append("")
            doc_lines.append("Args:")
            for fc in inputs:
                doc_lines.append(f"    {fc.name}: {fc.doc}")
        example = m.examples[0].values if m.examples else {}
        args = []
        json_args: dict[str, Any] = {}
        ok = True
        for fc, f in zip(inputs, [f for f in m.fields if f.role in DATA_ROLES], strict=True):
            if f.name in example:
                args.append(f"{fc.name}={_literal(example[f.name], fc)}")
                value = example[f.name]
                json_args[fc.name] = int(value) if fc.enum is not None and fc.enum.base == "IntEnum" else value
            else:
                ok = False
        sila_doc = (m.doc or f"Send {m.key}.") + f" Classification: {m.classification.value}."
        messages.append(
            MessageCtx(
                id=m.id,
                name=m.name,
                class_name=b.class_name(m.name),
                method=method,
                kind=m.kind.value,
                classification=m.classification.value,
                direction="tx",
                inputs=inputs,
                outputs=[],
                response=resp,
                return_type=return_type,
                return_kind=return_kind,
                doc="\n".join(doc_lines),
                wire=_wire(m, model),
                preconditions=pre_names,
                example_args=", ".join(args),
                example_ok=ok,
                example_json=repr(json_args),
                check_status=check_status,
                actuating=m.classification is Classification.ACTUATING,
                read_only=m.classification is Classification.READ_ONLY,
                sila_id=camel(m.name),
                sila_doc=sila_doc,
            )
        )
    ctx = GenContext(
        model=model,
        package=package,
        dist_name=package.replace("_", "-"),
        class_name=camel(base_name) + "Driver",
        title=base_name.replace("_", " ").title(),
        feature_id=camel(base_name) + "Instrument",
        transport_kind=transport.value,
        default_resource=default_resource,
        message_mode=transport in (Transport.HID, Transport.WINUSB),
        dependencies=deps,
        enums=b.enums,
        messages=messages,
        responses=list(responses.values()),
        calder_version=model.calder_version,
        warnings=list(model.warnings),
    )
    by_msg_id = {mc.id: mc for mc in messages}
    # Preconditions: an example message whose requirement is a single no-argument command.
    for m in active:
        if m.direction is Direction.TX and m.preconditions and m.id in by_msg_id:
            ctx.has_preconditions = True
            req = by_id.get(m.preconditions[0].message)
            if ctx.precondition_example is None and req is not None and req.id in by_msg_id:
                target = by_msg_id[m.id]
                if target.example_ok:
                    ctx.precondition_example = (target.method, target.example_args, by_msg_id[req.id].method)
    ctx.first_query = next(
        (mc for mc in messages if mc.read_only and not mc.inputs and mc.return_kind != "none" and not mc.preconditions),
        None,
    )
    ctx.first_actuating = next((mc for mc in messages if mc.actuating and mc.example_ok), None)
    # Aliases from UI buttons: one method per button, sending what the button sent.
    _aliases(ctx, model, by_msg_id, used_methods)
    for m in active:
        if m.kind.value == "unsolicited" and m.id in responses:
            rc = responses[m.id]
            ctx.streams.append(StreamCtx(name=ident(f"read_{m.name}"), message=rc))
    return ctx


def _aliases(ctx: GenContext, model: ProtocolModel, by_msg_id: dict[str, MessageCtx], used: set[str]) -> None:
    buttons: dict[str, list[tuple[MessageType, dict[str, str]]]] = {}
    names: dict[str, str] = {}
    for m in model.active_messages():
        if m.direction is not Direction.TX or m.id not in by_msg_id:
            continue
        for bnd in m.ui_bindings:
            if bnd.kind != "invoke":
                continue
            buttons.setdefault(bnd.element, []).append((m, bnd.constants))
            names[bnd.element] = bnd.element_name or bnd.element
    order = {ex.message: i for i, ex in reversed(list(enumerate(model.exchanges)))}
    for element, bound in sorted(buttons.items()):
        alias = ident(names[element])
        if alias in used:
            continue
        bound.sort(key=lambda mb: order.get(mb[0].id, 0))
        params: list[str] = []
        seen_params: set[str] = set()
        calls: list[tuple[str, str]] = []
        actuating = False
        ok = True
        for m, constants in bound:
            mc = by_msg_id[m.id]
            actuating = actuating or mc.actuating
            args = []
            for fc in mc.inputs:
                if fc.id in constants:
                    if fc.enum is None and fc.py_type not in ("float", "int", "str"):
                        ok = False
                        break
                    args.append(f"{fc.name}={_literal(constants[fc.id], fc)}")
                else:
                    if fc.name not in seen_params:
                        seen_params.add(fc.name)
                        params.append(f"{fc.name}: {fc.py_type}")
                    args.append(f"{fc.name}={fc.name}")
            calls.append((mc.method, ", ".join(args)))
        if not ok or not calls:
            continue
        if len(calls) == 1 and calls[0][0] == alias:
            continue
        last = by_msg_id[bound[-1][0].id]
        returns = last.return_type if len(calls) == 1 else "None"
        used.add(alias)
        targets = ", ".join(c[0] for c in calls)
        ctx.aliases.append(
            AliasCtx(
                name=alias,
                params=", ".join(params),
                call_args=calls,
                doc=f"What the vendor UI's '{names[element]}' control sent: {targets}.",
                actuating=actuating,
                returns=returns,
                return_expr="" if returns == "None" else "return ",
            )
        )
