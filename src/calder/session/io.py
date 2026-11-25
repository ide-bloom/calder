"""Reading and writing the ``.libsession`` container.

The container is a zip so a session stays one portable file that is easy to
attach to a bug report, while each part remains a plain text format that can be
inspected without Calder.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from calder.session.models import (
    SESSION_FORMAT_VERSION,
    Session,
    SessionMeta,
    UiEvent,
    WireEvent,
)

EVENTS_NAME = "events.jsonl"
UI_NAME = "ui.jsonl"
META_NAME = "meta.json"
NOTES_NAME = "notes.md"


class SessionFormatError(ValueError):
    """The file is not a readable session."""


def _jsonl(lines: list[str]) -> bytes:
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def dumps_session(session: Session) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(META_NAME, session.meta.model_dump_json(indent=2))
        zf.writestr(EVENTS_NAME, _jsonl([e.model_dump_json() for e in session.events]))
        zf.writestr(UI_NAME, _jsonl([e.model_dump_json() for e in session.ui_events]))
        zf.writestr(NOTES_NAME, session.notes)
    return buf.getvalue()


def save_session(session: Session, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(dumps_session(session))
    return target


def _read_jsonl(zf: zipfile.ZipFile, name: str) -> list[dict[str, object]]:
    if name not in zf.namelist():
        return []
    rows: list[dict[str, object]] = []
    for lineno, line in enumerate(zf.read(name).decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SessionFormatError(f"{name}:{lineno}: {exc}") from exc
        if not isinstance(value, dict):
            raise SessionFormatError(f"{name}:{lineno}: expected an object")
        rows.append(value)
    return rows


def loads_session(data: bytes) -> Session:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SessionFormatError("not a .libsession zip container") from exc
    with zf:
        if META_NAME not in zf.namelist():
            raise SessionFormatError("missing meta.json")
        meta = SessionMeta.model_validate_json(zf.read(META_NAME))
        major = meta.format_version.split(".")[0]
        if major != SESSION_FORMAT_VERSION.split(".")[0]:
            raise SessionFormatError(f"unsupported session format {meta.format_version}")
        events = [WireEvent.model_validate(r) for r in _read_jsonl(zf, EVENTS_NAME)]
        ui = [UiEvent.model_validate(r) for r in _read_jsonl(zf, UI_NAME)]
        notes = zf.read(NOTES_NAME).decode("utf-8") if NOTES_NAME in zf.namelist() else ""
    return Session(meta=meta, events=events, ui_events=ui, notes=notes)


def load_session(path: str | Path) -> Session:
    return loads_session(Path(path).read_bytes())


def session_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
