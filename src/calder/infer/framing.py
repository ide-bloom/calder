"""Framing discovery.

ASCII: choose the terminator that ends the vendor software's writes.
Binary: search start sequences and length fields (offset, width, endianness,
adjustment) that predict every frame boundary in every stream; fall back to
fixed-length frames, then to transport message boundaries.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from calder.infer.streams import Chunk, Frame, StreamSet
from calder.session.models import Direction

TERMINATORS: tuple[bytes, ...] = (b"\r\n", b"\n\r", b"\n", b"\r", b"\x03", b";", b"\x00")


@dataclass
class TerminatorResult:
    terminator: bytes
    chunk_end_ratio: float
    count: int
    alternatives: list[tuple[bytes, float, int]] = field(default_factory=list)


def detect_terminator(groups: list[list[Chunk]]) -> TerminatorResult | None:
    """Pick the terminator ending most chunks; ties prefer the longer sequence."""
    chunks = [c for g in groups for c in g]
    if not chunks:
        return None
    joined = b"".join(b"".join(c.data for c in g) for g in groups)
    scored: list[tuple[bytes, float, int]] = []
    for term in TERMINATORS:
        count = joined.count(term)
        if count == 0:
            continue
        ends = sum(1 for c in chunks if c.data.endswith(term)) / len(chunks)
        # Streams must end on a terminator once trailing partial data is excluded.
        tail_ok = all(b"".join(c.data for c in g).rstrip(b" ").endswith(term) for g in groups if g)
        scored.append((term, ends + (0.25 if tail_ok else 0.0), count))
    if not scored:
        return None
    # "\n" also ends every "\r\n"-terminated chunk; only keep the shorter one when
    # some "\n" is not preceded by "\r".
    if any(t == b"\r\n" for t, _, _ in scored):
        bare_lf = joined.count(b"\n") - joined.count(b"\r\n")
        if bare_lf == 0:
            scored = [s for s in scored if s[0] not in (b"\n", b"\r")]
    scored.sort(key=lambda s: (-s[1], -len(s[0]), -s[2]))
    best = scored[0]
    if best[1] < 0.5:
        return None
    return TerminatorResult(best[0], min(best[1], 1.0), best[2], scored[1:4])


def split_delimited(groups: list[list[Chunk]], terminator: bytes, direction: Direction) -> list[Frame]:
    frames: list[Frame] = []
    for group in groups:
        buf = bytearray()
        events: list[int] = []
        t_start = 0
        for c in group:
            if not buf:
                t_start = c.t_ns
                events = []
            buf.extend(c.data)
            events.append(c.index)
            while True:
                idx = buf.find(terminator)
                if idx < 0:
                    break
                raw = bytes(buf[: idx + len(terminator)])
                del buf[: idx + len(terminator)]
                if raw.strip(b"\r\n\x00 ") or len(raw) > len(terminator):
                    frames.append(Frame(c.session, direction, t_start, raw, tuple(events), c.connection))
                t_start = c.t_ns
                events = [c.index]
    return frames


# ----- binary ----------------------------------------------------------------------


@dataclass(frozen=True)
class LengthCandidate:
    start: bytes
    offset: int
    width: int
    endian: str
    adjust: int
    frames: int
    aligned: float
    precision: float = 1.0

    @property
    def agreement(self) -> float:
        """How well frame boundaries and read/write boundaries agree, both ways."""
        return self.aligned * self.precision

    def describe(self) -> str:
        return (
            f"start={self.start.hex() or '-'} length@{self.offset} w{self.width} {self.endian} adjust {self.adjust:+d}"
        )


def start_candidates(groups: list[list[Chunk]], min_ratio: float = 0.9) -> list[bytes]:
    chunks = [c for g in groups for c in g if c.data]
    if not chunks:
        return []
    out: list[bytes] = []
    for n in (2, 1):
        counts = Counter(c.data[:n] for c in chunks if len(c.data) >= n)
        if not counts:
            continue
        value, count = counts.most_common(1)[0]
        if count / len(chunks) >= min_ratio and value not in out:
            out.append(value)
    return out


def _parse_lengths(data: bytes, start: bytes, offset: int, width: int, endian: str, adjust: int) -> list[int] | None:
    pos = 0
    order: str = "little" if endian == "le" else "big"
    lengths: list[int] = []
    minimum = max(offset + width, len(start), 2)
    while pos < len(data):
        if start and not data.startswith(start, pos):
            return None
        if pos + offset + width > len(data):
            return None
        total = int.from_bytes(data[pos + offset : pos + offset + width], order) + adjust  # type: ignore[arg-type]
        if total < minimum or pos + total > len(data):
            return None
        lengths.append(total)
        pos += total
    return lengths


def search_length_field(
    groups_by_direction: list[list[list[Chunk]]],
    *,
    max_offset: int = 8,
    adjust_range: range = range(-4, 24),
) -> tuple[LengthCandidate | None, list[LengthCandidate]]:
    """Find length-field framing consistent with every stream in every direction."""
    streams: list[tuple[bytes, list[Chunk]]] = []
    for groups in groups_by_direction:
        for g in groups:
            if g:
                streams.append((b"".join(c.data for c in g), g))
    if not streams:
        return None, []
    # Host writes usually carry whole frames, so they reveal a start sequence even
    # when instrument reads are split arbitrarily; every stream must still parse.
    starts = [*start_candidates(groups_by_direction[0]), b""]
    valid: list[LengthCandidate] = []
    for start in starts:
        for offset in range(len(start), max_offset):
            for width, endian in ((1, "le"), (2, "le"), (2, "be"), (4, "le"), (4, "be")):
                for adjust in adjust_range:
                    all_lengths: list[int] = []
                    aligned_hits = 0
                    total_chunks = 0
                    precise_hits = 0
                    ok = True
                    for data, group in streams:
                        lengths = _parse_lengths(data, start, offset, width, endian, adjust)
                        if lengths is None:
                            ok = False
                            break
                        all_lengths.extend(lengths)
                        boundaries: set[int] = set()
                        pos = 0
                        for n in lengths:
                            pos += n
                            boundaries.add(pos)
                        pos = 0
                        chunk_ends: set[int] = set()
                        for c in group:
                            pos += len(c.data)
                            total_chunks += 1
                            chunk_ends.add(pos)
                            aligned_hits += pos in boundaries
                        precise_hits += len(boundaries & chunk_ends)
                    if not ok or len(set(all_lengths)) < 2:
                        continue
                    valid.append(
                        LengthCandidate(
                            start,
                            offset,
                            width,
                            endian,
                            adjust,
                            len(all_lengths),
                            aligned_hits / max(total_chunks, 1),
                            precise_hits / max(len(all_lengths), 1),
                        )
                    )
    if not valid:
        return None, []
    # Prefer boundaries that coincide with the vendor's reads and writes, then a
    # one-byte start sequence (a second constant byte is usually an address), narrow
    # fields and early offsets.
    valid.sort(key=lambda c: (-round(c.agreement, 2), not c.start, len(c.start), c.width, c.offset, abs(c.adjust)))
    best = valid[0]
    # A one-byte length preceded by an always-zero byte is the low byte of a wider
    # big-endian field; take the wider reading so longer frames still parse.
    if best.width == 1:
        for cand in valid:
            wide = (best.start, best.offset - 1, 2, "be", best.adjust)
            if (cand.start, cand.offset, cand.width, cand.endian, cand.adjust) == wide:
                best = cand
                break
    return best, [c for c in valid if c is not best][:5]


def fixed_length(groups_by_direction: list[list[list[Chunk]]]) -> int | None:
    lengths = {len(c.data) for groups in groups_by_direction for g in groups for c in g}
    if len(lengths) == 1:
        n = lengths.pop()
        return n if n > 1 else None
    return None


def split_by_length(groups: list[list[Chunk]], direction: Direction, cand: LengthCandidate) -> list[Frame]:
    order: str = "little" if cand.endian == "le" else "big"
    frames: list[Frame] = []
    for g in groups:
        buf = bytearray()
        events: list[int] = []
        t_start = 0
        for c in g:
            if not buf:
                t_start = c.t_ns
                events = []
            buf.extend(c.data)
            events.append(c.index)
            while len(buf) >= cand.offset + cand.width:
                total = int.from_bytes(buf[cand.offset : cand.offset + cand.width], order) + cand.adjust  # type: ignore[arg-type]
                if len(buf) < total or total <= 0:
                    break
                frames.append(Frame(c.session, direction, t_start, bytes(buf[:total]), tuple(events), c.connection))
                del buf[:total]
                t_start = c.t_ns
                events = [c.index]
    return frames


def split_fixed(groups: list[list[Chunk]], direction: Direction, size: int) -> list[Frame]:
    frames: list[Frame] = []
    for g in groups:
        buf = bytearray()
        events: list[int] = []
        t_start = 0
        for c in g:
            if not buf:
                t_start, events = c.t_ns, []
            buf.extend(c.data)
            events.append(c.index)
            while len(buf) >= size:
                frames.append(Frame(c.session, direction, t_start, bytes(buf[:size]), tuple(events), c.connection))
                del buf[:size]
                t_start, events = c.t_ns, [c.index]
    return frames


def split_messages(groups: list[list[Chunk]], direction: Direction, gap_ns: int = 0) -> list[Frame]:
    """One frame per chunk, merging chunks closer than ``gap_ns`` (inter-byte silence)."""
    frames: list[Frame] = []
    for g in groups:
        current: Frame | None = None
        last_t = 0
        for c in g:
            if current is not None and gap_ns and c.t_ns - last_t < gap_ns:
                current.raw += c.data
                current.events = (*current.events, c.index)
            else:
                current = Frame(c.session, direction, c.t_ns, c.data, (c.index,), c.connection)
                frames.append(current)
            last_t = c.t_ns
    return frames


def gap_threshold_ns(baudrate: int | None) -> int:
    """Modbus-style silent interval: 3.5 character times, at least 2 ms."""
    if not baudrate:
        return 4_000_000
    char_ns = 11 * 1_000_000_000 // baudrate
    return max(int(3.5 * char_ns), 2_000_000)


def order_frames(frames: list[Frame]) -> list[Frame]:
    ordered = sorted(frames, key=lambda f: (f.session, f.t_ns, f.direction is Direction.RX, f.events))
    for i, f in enumerate(ordered):
        f.seq = i
    return ordered


def stream_groups(streams: StreamSet) -> tuple[list[list[Chunk]], list[list[Chunk]]]:
    return streams.groups(Direction.TX), streams.groups(Direction.RX)
