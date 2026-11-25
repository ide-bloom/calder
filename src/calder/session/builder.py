"""Incremental construction of sessions from any capture source."""

from __future__ import annotations

import uuid
from typing import Any

from calder import __version__
from calder.session.models import (
    Direction,
    EventKind,
    Session,
    SessionMeta,
    Source,
    Transport,
    UiElement,
    UiEvent,
    UiKind,
    WireEvent,
)


class SessionBuilder:
    """Collects events and assigns indices and connection numbers in arrival order."""

    def __init__(self, source: Source, *, session_id: str | None = None, **meta: Any) -> None:
        self.meta = SessionMeta(
            session_id=session_id or uuid.uuid4().hex,
            calder_version=__version__,
            source=source,
            **meta,
        )
        self._events: list[WireEvent] = []
        self._ui: list[UiEvent] = []
        self._connections: dict[str, int] = {}
        self.notes = ""

    def _connection(self, stream: str, kind: EventKind) -> int:
        if kind is EventKind.OPEN:
            self._connections[stream] = self._connections.get(stream, -1) + 1
        return max(self._connections.get(stream, 0), 0)

    def wire(
        self,
        t_ns: int,
        transport: Transport,
        stream: str,
        direction: Direction = Direction.NONE,
        data: bytes = b"",
        kind: EventKind = EventKind.DATA,
        *,
        pid: int = 0,
        tid: int = 0,
        meta: dict[str, Any] | None = None,
    ) -> WireEvent:
        event = WireEvent(
            index=len(self._events),
            t_ns=max(t_ns, 0),
            transport=transport,
            stream=stream,
            direction=direction,
            kind=kind,
            data=data,
            connection=self._connection(stream, kind),
            pid=pid,
            tid=tid,
            meta=meta or {},
        )
        self._events.append(event)
        if transport not in self.meta.transports:
            self.meta.transports.append(transport)
        return event

    def ui(self, t_ns: int, kind: UiKind, element: UiElement, value: str | None = None) -> UiEvent:
        event = UiEvent(index=len(self._ui), t_ns=max(t_ns, 0), kind=kind, element=element, value=value)
        self._ui.append(event)
        return event

    def build(self) -> Session:
        events = sorted(self._events, key=lambda e: (e.t_ns, e.index))
        ui = sorted(self._ui, key=lambda e: (e.t_ns, e.index))
        events = [e.model_copy(update={"index": i}) for i, e in enumerate(events)]
        ui = [e.model_copy(update={"index": i}) for i, e in enumerate(ui)]
        return Session(meta=self.meta, events=events, ui_events=ui, notes=self.notes)
