"""Pairwise and progressive multiple sequence alignment of messages.

Needleman-Wunsch global alignment (Needleman & Wunsch, 1970) with a
UPGMA guide tree and profile-profile merging, in the style of the
progressive methods used for protocol reverse engineering (Beddoe's Protocol
Informatics project; Cui et al., Discoverer, 2007).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

MATCH = 2
MISMATCH = -1
GAP = -2

Column = tuple[int | None, ...]


def nw_align(a: Sequence[int], b: Sequence[int]) -> list[tuple[int | None, int | None]]:
    """Global alignment of two byte sequences; ``None`` marks a gap."""
    n, m = len(a), len(b)
    score = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0] = i * GAP
    for j in range(1, m + 1):
        score[0][j] = j * GAP
    for i in range(1, n + 1):
        ai = a[i - 1]
        row, prev = score[i], score[i - 1]
        for j in range(1, m + 1):
            diag = prev[j - 1] + (MATCH if ai == b[j - 1] else MISMATCH)
            row[j] = max(diag, prev[j] + GAP, row[j - 1] + GAP)
    out: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and score[i][j] == score[i - 1][j - 1] + (MATCH if a[i - 1] == b[j - 1] else MISMATCH):
            out.append((a[i - 1], b[j - 1]))
            i, j = i - 1, j - 1
        elif i > 0 and score[i][j] == score[i - 1][j] + GAP:
            out.append((a[i - 1], None))
            i -= 1
        else:
            out.append((None, b[j - 1]))
            j -= 1
    out.reverse()
    return out


def identity(a: Sequence[int], b: Sequence[int]) -> float:
    pairs = nw_align(a, b)
    if not pairs:
        return 1.0
    return sum(1 for x, y in pairs if x is not None and x == y) / len(pairs)


def _column_score(c1: Column, c2: Column) -> float:
    total = 0.0
    count = 0
    for x in c1:
        for y in c2:
            count += 1
            if x is None and y is None:
                continue
            if x is None or y is None:
                total += GAP
            else:
                total += MATCH if x == y else MISMATCH
    return total / count if count else 0.0


def align_profiles(p1: list[Column], p2: list[Column]) -> list[Column]:
    """Align two profiles (lists of columns) and return the merged profile."""
    n, m = len(p1), len(p2)
    w1 = len(p1[0]) if p1 else 0
    w2 = len(p2[0]) if p2 else 0
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0] = i * GAP
    for j in range(1, m + 1):
        score[0][j] = j * GAP
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            score[i][j] = max(
                score[i - 1][j - 1] + _column_score(p1[i - 1], p2[j - 1]),
                score[i - 1][j] + GAP,
                score[i][j - 1] + GAP,
            )
    gap1: Column = (None,) * w1
    gap2: Column = (None,) * w2
    out: list[Column] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and abs(score[i][j] - (score[i - 1][j - 1] + _column_score(p1[i - 1], p2[j - 1]))) < 1e-9:
            out.append(p1[i - 1] + p2[j - 1])
            i, j = i - 1, j - 1
        elif i > 0 and abs(score[i][j] - (score[i - 1][j] + GAP)) < 1e-9:
            out.append(p1[i - 1] + gap2)
            i -= 1
        else:
            out.append(gap1 + p2[j - 1])
            j -= 1
    out.reverse()
    return out


@dataclass
class Alignment:
    """Rows are the input sequences with ``None`` gaps; ``order`` maps rows to inputs."""

    columns: list[Column]
    order: list[int]

    def rows(self) -> list[list[int | None]]:
        rows: list[list[int | None]] = [[] for _ in self.order]
        for col in self.columns:
            for r, v in enumerate(col):
                rows[r].append(v)
        by_input: list[list[int | None]] = [[] for _ in self.order]
        for r, idx in enumerate(self.order):
            by_input[idx] = rows[r]
        return by_input

    def constant_columns(self) -> list[bool]:
        return [all(v is not None for v in col) and len(set(col)) == 1 for col in self.columns]


def progressive_align(seqs: Sequence[bytes]) -> Alignment:
    """Progressive MSA: UPGMA guide tree over pairwise identity, then profile merging."""
    if not seqs:
        return Alignment([], [])
    clusters: dict[int, tuple[list[Column], list[int]]] = {i: ([(b,) for b in s], [i]) for i, s in enumerate(seqs)}
    if len(seqs) == 1:
        cols, order = clusters[0]
        return Alignment(cols, order)
    dist: dict[tuple[int, int], float] = {}
    keys = list(clusters)
    for x in range(len(keys)):
        for y in range(x + 1, len(keys)):
            dist[(keys[x], keys[y])] = 1.0 - identity(seqs[keys[x]], seqs[keys[y]])
    sizes = dict.fromkeys(keys, 1)
    next_id = len(seqs)
    while len(clusters) > 1:
        (a, b), _ = min(dist.items(), key=lambda kv: (kv[1], kv[0]))
        pa, oa = clusters.pop(a)
        pb, ob = clusters.pop(b)
        merged = align_profiles(pa, pb)
        new = next_id
        next_id += 1
        clusters[new] = (merged, oa + ob)
        new_dist: dict[tuple[int, int], float] = {}
        for (x, y), d in dist.items():
            if a in (x, y) or b in (x, y):
                continue
            new_dist[(x, y)] = d
        for k in clusters:
            if k == new:
                continue
            da = dist.get((min(a, k), max(a, k)), 1.0)
            db = dist.get((min(b, k), max(b, k)), 1.0)
            new_dist[(min(k, new), max(k, new))] = (da * sizes[a] + db * sizes[b]) / (sizes[a] + sizes[b])
        sizes[new] = sizes[a] + sizes[b]
        dist = new_dist
    (cols, order) = next(iter(clusters.values()))
    # order[r] is the input index of row r; Alignment expects that mapping.
    return Alignment(cols, order)


def field_spans(alignment: Alignment) -> list[tuple[int, int, bool]]:
    """Group adjacent columns into (start, end, constant) spans."""
    spans: list[tuple[int, int, bool]] = []
    flags = alignment.constant_columns()
    start = 0
    for i in range(1, len(flags) + 1):
        if i == len(flags) or flags[i] != flags[start] or flags[start]:
            spans.append((start, i, flags[start]))
            start = i
    return spans
