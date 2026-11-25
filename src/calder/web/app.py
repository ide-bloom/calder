"""Local review UI: inspect, rename, retype, accept or reject inferred elements.

Bound to 127.0.0.1 on a random port; every API request must carry the
per-launch token, and the Host header must name the loopback address (a
DNS-rebinding guard). Edits are validated and saved back to the model with
an audit entry.
"""

from __future__ import annotations

import getpass
import secrets
import socket
import threading
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ValidationError

from calder.infer.correlate import snake
from calder.model.io import ModelValidationError, load_model, save_model, validate_model
from calder.model.schema import (
    AuditEntry,
    Classification,
    EncodingModel,
    ProtocolModel,
    Status,
)
from calder.session.io import SessionFormatError, load_session

TOKEN_HEADER = "x-calder-token"  # noqa: S105 - a header name, not a secret


class Edit(BaseModel):
    target: str
    action: str
    value: Any = None
    note: str = ""


def _asset(name: str) -> str:
    return resources.files("calder.web").joinpath("static", name).read_text(encoding="utf-8")


def render_index(token: str) -> str:
    html = _asset("index.html")
    return (
        html.replace("/*__CSS__*/", _asset("style.css"))
        .replace("/*__JS__*/", _asset("app.js"))
        .replace("__TOKEN__", token)
    )


class ReviewState:
    def __init__(self, model_path: Path, actor: str) -> None:
        self.model_path = model_path
        self.actor = actor
        self.lock = threading.Lock()
        self.model = load_model(model_path)

    def timeline(self, limit: int) -> list[dict[str, Any]]:
        msg_by_event: dict[tuple[str, int], str] = {}
        for ex in self.model.exchanges:
            msg_by_event[(ex.session, ex.index)] = ex.message
        rows: list[dict[str, Any]] = []
        base = self.model_path.parent
        for ref in self.model.sessions:
            if not ref.path:
                continue
            path = Path(ref.path)
            if not path.is_absolute():
                path = base / path
            try:
                session = load_session(path)
            except (OSError, SessionFormatError):
                continue
            streams = {self.model.stream} | {e.stream for e in session.events if e.kind.value == "data"}
            busiest = max(streams, key=lambda n: sum(len(e.data) for e in session.events if e.stream == n))
            for e in session.events:
                if e.stream not in (self.model.stream, busiest):
                    continue
                printable = (
                    e.data.decode("latin-1")
                    if e.data and all(32 <= b < 127 or b in (9, 10, 13) for b in e.data)
                    else None
                )
                rows.append(
                    {
                        "session": ref.id,
                        "t": e.t_ns,
                        "source": "wire",
                        "index": e.index,
                        "kind": e.kind.value,
                        "direction": e.direction.value,
                        "hex": e.data.hex(),
                        "text": printable,
                        "meta": e.meta,
                        "message": msg_by_event.get((ref.id, e.index)),
                    }
                )
            for u in session.ui_events:
                rows.append(
                    {
                        "session": ref.id,
                        "t": u.t_ns,
                        "source": "ui",
                        "index": u.index,
                        "kind": u.kind.value,
                        "element": u.element.model_dump(),
                        "value": u.value,
                    }
                )
        rows.sort(key=lambda r: (r["session"], r["t"]))
        return rows[:limit]

    def apply(self, edit: Edit) -> dict[str, Any]:
        with self.lock:
            candidate = self.model.model_copy(deep=True)
            before, after = _apply(candidate, edit)
            candidate.audit.append(
                AuditEntry(
                    actor=self.actor, action=edit.action, target=edit.target, before=before, after=after, note=edit.note
                )
            )
            validate_model(candidate)
            save_model(candidate, self.model_path)
            self.model = candidate
            return {"before": before, "after": after}


def _ident(value: Any) -> str:
    text = str(value or "").strip()
    name = snake(text)
    if not text or name != text:
        raise ModelValidationError(f"{text!r} is not a snake_case identifier (try {name!r})")
    return name


def _apply(model: ProtocolModel, edit: Edit) -> tuple[Any, Any]:
    kind, _, ref = edit.target.partition(":")
    action = edit.action
    if kind == "message":
        msg = model.message(ref)
        if action == "rename":
            before, msg.name = msg.name, _ident(edit.value)
            msg.status = Status.EDITED
            return before, msg.name
        if action == "adopt_suggestion":
            if not msg.suggested_name:
                raise ModelValidationError("no suggested name")
            before, msg.name = msg.name, msg.suggested_name
            return before, msg.name
        if action in ("accept", "reject"):
            before, msg.status = msg.status.value, Status.ACCEPTED if action == "accept" else Status.REJECTED
            return before, msg.status.value
        if action == "classify":
            before = msg.classification.value
            msg.classification = Classification(str(edit.value))
            msg.classification_confirmed = True
            msg.classification_reason = "set by the user in review"
            return before, msg.classification.value
        if action == "confirm_classification":
            msg.classification_confirmed = True
            return False, True
        if action == "document":
            before, msg.doc = msg.doc, str(edit.value or "")
            return before, msg.doc
    elif kind == "field":
        mid, _, fid = ref.partition("/")
        fld = model.message(mid).field(fid)
        if action == "rename":
            before, fld.name = fld.name, _ident(edit.value)
            fld.status = Status.EDITED
            return before, fld.name
        if action == "retype":
            if not isinstance(edit.value, dict):
                raise ModelValidationError("retype needs an encoding object")
            before_enc = fld.encoding.model_dump(mode="json") if fld.encoding else None
            fld.encoding = EncodingModel.model_validate(edit.value)
            fld.status = Status.EDITED
            return before_enc, fld.encoding.model_dump(mode="json")
        if action in ("accept", "reject"):
            before, fld.status = fld.status.value, Status.ACCEPTED if action == "accept" else Status.REJECTED
            return before, fld.status.value
        if action == "units":
            before_units = fld.units
            fld.units = str(edit.value) if edit.value else None
            return before_units, fld.units
    elif kind == "precondition":
        mid, _, req = ref.partition("/")
        msg = model.message(mid)
        for p in msg.preconditions:
            if p.message == req and action in ("accept", "reject"):
                before, p.status = p.status.value, Status.ACCEPTED if action == "accept" else Status.REJECTED
                return before, p.status.value
        raise ModelValidationError(f"no precondition {req} on {mid}")
    raise ModelValidationError(f"unsupported edit {action!r} on {edit.target!r}")


def create_app(model_path: str | Path, token: str, *, actor: str | None = None) -> FastAPI:
    state = ReviewState(Path(model_path), actor or _actor())
    app = FastAPI(title="Calder review", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.review = state

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if request.url.path.startswith("/api/"):
            supplied = request.headers.get(TOKEN_HEADER, "")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse({"detail": "missing or invalid token"}, status_code=403)
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(t: str = "") -> HTMLResponse:
        if not secrets.compare_digest(t, token):
            raise HTTPException(status_code=403, detail="open the URL printed by `calder review`")
        return HTMLResponse(render_index(token))

    @app.get("/api/model")
    def get_model() -> JSONResponse:
        return JSONResponse(state.model.model_dump(mode="json"))

    @app.get("/api/timeline")
    def get_timeline(limit: int = 5000) -> JSONResponse:
        return JSONResponse(state.timeline(max(1, min(limit, 50000))))

    @app.post("/api/edit")
    def post_edit(edit: Edit) -> JSONResponse:
        try:
            result = state.apply(edit)
        except (ModelValidationError, KeyError, ValueError, ValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, **result, "model": state.model.model_dump(mode="json")})

    return app


def _actor() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return "user"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def serve(model_path: str | Path, *, port: int = 0, open_browser: bool = True) -> None:
    """Run the review UI until interrupted."""
    token = secrets.token_urlsafe(24)
    app = create_app(model_path, token)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", port))
    actual = sock.getsockname()[1]
    url = f"http://127.0.0.1:{actual}/?t={token}"
    print(f"Calder review: {url}", flush=True)
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    config = uvicorn.Config(app, log_level="warning")
    uvicorn.Server(config).run(sockets=[sock])
