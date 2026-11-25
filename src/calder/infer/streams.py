"""Stream reconstruction: per-resource, per-direction byte chunks from sessions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from calder.session.models import Direction, EventKind, Session, Transport

PRINTABLE = frozenset(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}
MESSAGE_TRANSPORTS = frozenset({Transport.HID, Transport.WINUSB})


@dataclass(frozen=True)
class Chunk:
    """One data event: a single read or write as the vendor software issued it."""

    session: int
    index: int
    t_ns: int
    direction: Direction
    connection: int
    data: bytes


@dataclass
class Frame:
    """One protocol message reconstructed from one or more chunks."""

    session: int
    direction: Direction
    t_ns: int
    raw: bytes
    events: tuple[int, ...]
    connection: int = 0
    seq: int = 0
    type_id: str | None = None
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def tx(self) -> bool:
        return self.direction is Direction.TX


@dataclass
class StreamSet:
    """All chunks of one resource across the sessions being inferred."""

    stream: str
    transport: Transport
    sessions: list[Session]
    chunks: list[Chunk]
    names: list[str] = field(default_factory=list)  # the stream's name in each session

    def groups(self, direction: Direction) -> list[list[Chunk]]:
        """Chunks split by session and connection, in time order."""
        out: dict[tuple[int, int], list[Chunk]] = defaultdict(list)
        for c in self.chunks:
            if c.direction is direction:
                out[(c.session, c.connection)].append(c)
        return [sorted(v, key=lambda c: (c.t_ns, c.index)) for _, v in sorted(out.items())]

    def total_bytes(self) -> int:
        return sum(len(c.data) for c in self.chunks)

    @property
    def message_oriented(self) -> bool:
        return self.transport in MESSAGE_TRANSPORTS


def candidate_streams(sessions: list[Session]) -> list[tuple[str, int]]:
    """Streams ranked by bytes carried, with the byte count."""
    totals: dict[str, int] = defaultdict(int)
    directions: dict[str, set[Direction]] = defaultdict(set)
    for s in sessions:
        for e in s.events:
            if e.kind is EventKind.DATA and e.data:
                totals[e.stream] += len(e.data)
                directions[e.stream].add(e.direction)
    # Prefer bidirectional streams: a one-way stream is rarely the instrument link.
    return sorted(totals.items(), key=lambda kv: (len(directions[kv[0]]) < 2, -kv[1], kv[0]))


def collect(sessions: list[Session], stream: str | None = None) -> StreamSet:
    """Chunks of the instrument stream across sessions.

    Without ``stream``, each session contributes its own busiest bidirectional stream
    of the transport chosen for the first session: recordings of the same instrument
    often name the resource differently (another COM port, pipe or address).
    """
    ranked = candidate_streams(sessions)
    if not ranked:
        raise ValueError("the sessions contain no data events")
    per_session: list[str | None] = []
    if stream is None:
        chosen = ranked[0][0]
        kind = chosen.split(":", 1)[0]
        for s in sessions:
            own = [name for name, _ in candidate_streams([s]) if name.split(":", 1)[0] == kind]
            per_session.append(own[0] if own else None)
        chosen = next((name for name in per_session if name), chosen)
    else:
        matches = [name for name, _ in ranked if stream == name or stream in name]
        if not matches:
            raise ValueError(f"no stream matches {stream!r}; available: {[n for n, _ in ranked]}")
        chosen = matches[0]
        per_session = [chosen] * len(sessions)
    chunks: list[Chunk] = []
    transport: Transport | None = None
    for si, s in enumerate(sessions):
        name = per_session[si]
        for e in s.events:
            if e.stream != name or e.kind is not EventKind.DATA or not e.data:
                continue
            if e.direction is Direction.NONE:
                continue
            transport = e.transport
            chunks.append(Chunk(si, e.index, e.t_ns, e.direction, e.connection, e.data))
    if transport is None:
        raise ValueError(f"stream {chosen!r} has no directional data")
    return StreamSet(
        stream=chosen, transport=transport, sessions=sessions, chunks=chunks, names=[n or "" for n in per_session]
    )


def is_ascii(data: bytes, threshold: float = 0.97) -> bool:
    if not data:
        return False
    printable = sum(1 for b in data if b in PRINTABLE)
    return printable / len(data) >= threshold


def printable_ratio(data: bytes) -> float:
    return sum(1 for b in data if b in PRINTABLE) / len(data) if data else 0.0
