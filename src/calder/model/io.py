"""Loading, saving, validating and converting protocol models."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from calder.model.schema import (
    MODEL_KIND,
    SCHEMA_VERSION,
    FieldRole,
    ProtocolModel,
    Status,
)
from calder.runtime.protocol import RUNTIME_FORMAT


class ModelValidationError(ValueError):
    """The model is structurally valid JSON but internally inconsistent."""


def load_model(path: str | Path) -> ProtocolModel:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("kind") != MODEL_KIND:
        raise ModelValidationError(f"not a Calder protocol model (kind={data.get('kind')!r})")
    major = str(data.get("schema_version", "")).split(".")[0]
    if major != SCHEMA_VERSION.split(".")[0]:
        raise ModelValidationError(f"unsupported schema version {data.get('schema_version')}")
    model = ProtocolModel.model_validate(data)
    validate_model(model)
    return model


def dumps_model(model: ProtocolModel) -> str:
    return model.model_dump_json(indent=2) + "\n"


def save_model(model: ProtocolModel, path: str | Path) -> Path:
    validate_model(model)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps_model(model), encoding="utf-8")
    return target


def model_json_schema() -> dict[str, Any]:
    schema = ProtocolModel.model_json_schema()
    schema["$id"] = f"https://github.com/ide-bloom/calder/schema/protocol-{SCHEMA_VERSION}.json"
    schema["title"] = "Calder protocol model"
    return schema


def validate_model(model: ProtocolModel) -> None:
    """Check references between elements; raises :class:`ModelValidationError`."""
    ids = [m.id for m in model.messages]
    if len(ids) != len(set(ids)):
        raise ModelValidationError("duplicate message ids")
    names = [m.name for m in model.active_messages()]
    if len(names) != len(set(names)):
        raise ModelValidationError("duplicate message names")
    known = set(ids)
    for m in model.messages:
        field_ids = [f.id for f in m.fields]
        if len(field_ids) != len(set(field_ids)):
            raise ModelValidationError(f"{m.id}: duplicate field ids")
        field_names = [f.name for f in m.fields]
        if len(field_names) != len(set(field_names)):
            raise ModelValidationError(f"{m.id}: duplicate field names")
        if m.response is not None and m.response not in known:
            raise ModelValidationError(f"{m.id}: unknown response {m.response}")
        for p in m.preconditions:
            if p.message not in known:
                raise ModelValidationError(f"{m.id}: precondition on unknown message {p.message}")
            for d in p.disabled_by:
                if d not in known:
                    raise ModelValidationError(f"{m.id}: unknown disabling message {d}")
        for f in m.fields:
            if f.role in (FieldRole.VALUE, FieldRole.ENUM, FieldRole.STATUS) and f.encoding is None:
                raise ModelValidationError(f"{m.id}.{f.id}: {f.role} field needs an encoding")
            if f.reflects is not None:
                target_msg, target_field = f.reflects
                if target_msg not in known:
                    raise ModelValidationError(f"{m.id}.{f.id}: reflects unknown message")
                if target_field not in {x.id for x in model.message(target_msg).fields}:
                    raise ModelValidationError(f"{m.id}.{f.id}: reflects unknown field")
            if m.layout == "binary" and f.role is not FieldRole.CHECKSUM and (f.offset is None or f.length is None):
                raise ModelValidationError(f"{m.id}.{f.id}: binary field needs offset and length")
    for ex in model.exchanges:
        if ex.message not in known:
            raise ModelValidationError(f"exchange references unknown message {ex.message}")


def model_digest(model: ProtocolModel) -> str:
    return hashlib.sha256(dumps_model(model).encode("utf-8")).hexdigest()


def to_runtime(model: ProtocolModel) -> dict[str, Any]:
    """Project the model onto the runtime protocol format embedded in drivers.

    Rejected messages are dropped here, which is what keeps them out of every
    generated driver and out of ``verify``.
    """
    active = model.active_messages()
    active_ids = {m.id for m in active}
    transport: dict[str, Any] = {
        "kind": model.transport.kind.value,
        "resource": model.transport.resource,
        "settings": {
            **(model.transport.serial.model_dump() if model.transport.serial else {}),
            **model.transport.settings,
        },
    }
    framing = model.framing
    messages: list[dict[str, Any]] = []
    for m in active:
        fields: list[dict[str, Any]] = []
        for f in m.fields:
            if f.status is Status.REJECTED and f.role in (FieldRole.VALUE, FieldRole.ENUM):
                continue
            fields.append(
                {
                    "id": f.id,
                    "name": f.name,
                    "role": f.role.value,
                    "offset": f.offset,
                    "length": f.length,
                    "token": f.token,
                    "encoding": f.encoding.model_dump(mode="json") if f.encoding else None,
                    "constant": f.constant,
                    "enum": {e.wire: e.label for e in f.enum},
                    "units": f.units,
                    "label": f.label,
                    "minimum": f.range.minimum if f.range else None,
                    "maximum": f.range.maximum if f.range else None,
                    "echo_of": f.echo_of,
                    "reflects": list(f.reflects) if f.reflects and f.reflects[0] in active_ids else None,
                    "status_ok": f.status_ok,
                    "doc": f.doc,
                }
            )
        messages.append(
            {
                "id": m.id,
                "name": m.name,
                "direction": m.direction.value,
                "kind": m.kind.value,
                "key": m.key,
                "layout": m.layout,
                "header": m.header,
                "header_sep": m.header_sep,
                "arg_sep": m.arg_sep,
                "length": m.length,
                "fields": fields,
                "response": m.response if m.response in active_ids else None,
                "response_timeout_ms": m.response_timeout_ms,
                "classification": m.classification.value,
                "preconditions": [
                    {"message": p.message, "disabled_by": [d for d in p.disabled_by if d in active_ids]}
                    for p in m.preconditions
                    if p.status is not Status.REJECTED and p.message in active_ids
                ],
                "doc": m.doc,
            }
        )
    return {
        "format": RUNTIME_FORMAT,
        "name": model.name,
        "family": model.family.value,
        "encoding": framing.encoding,
        "transport": transport,
        "framing": {
            "kind": framing.kind.value,
            "terminator_tx": framing.terminator_tx,
            "terminator_rx": framing.terminator_rx,
            "start": framing.start,
            "length_field": (
                {
                    "offset": framing.length_field.offset,
                    "width": framing.length_field.width,
                    "endian": framing.length_field.endian,
                    "adjust": framing.length_field.adjust,
                }
                if framing.length_field
                else None
            ),
            "frame_length": framing.frame_length,
            "max_frame": 4096,
            "gap_ms": framing.gap_ms or 0.0,
        },
        "checksum": (
            model.checksum.model_dump(mode="json", exclude={"evidence", "covered"})
            | {"extra": {"covered": model.checksum.covered} if model.checksum.covered != "raw" else {}}
            if model.checksum
            else None
        ),
        "messages": messages,
        "exchanges": [
            e.model_dump(mode="json")
            for e in model.exchanges
            if e.message in active_ids and (e.response_message is None or e.response_message in active_ids)
        ],
        "unsolicited": model.unsolicited,
        "model_sha256": model_digest(model),
    }
