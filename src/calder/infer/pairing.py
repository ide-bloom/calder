"""Pairing requests with responses by timing.

For each request the candidate responses are the instrument frames that arrive
after it and before the next request (bounded by ``window_ns``). Per request
type, the majority shape of the first candidate is the response shape; each
request is then paired with its first candidate of that shape, which skips
streamed frames that happen to arrive first.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from calder.infer.streams import Frame

DEFAULT_WINDOW_NS = 2_000_000_000


@dataclass
class Pairing:
    responses: dict[int, Frame]  # request seq -> response frame
    rate: dict[str, float]  # request type -> fraction of requests answered
    shape: dict[str, str]  # request type -> response shape
    latency_ns: dict[str, list[int]]


def pair(
    frames: list[Frame],
    shape_of: Callable[[Frame], str],
    *,
    window_ns: int = DEFAULT_WINDOW_NS,
) -> Pairing:
    by_session: dict[tuple[int, int], list[Frame]] = defaultdict(list)
    for f in frames:
        by_session[(f.session, f.connection)].append(f)
    candidates: dict[int, list[Frame]] = {}
    requests: list[Frame] = []
    for seq in by_session.values():
        seq.sort(key=lambda f: (f.t_ns, f.seq))
        for i, f in enumerate(seq):
            if not f.tx:
                continue
            requests.append(f)
            cands: list[Frame] = []
            for g in seq[i + 1 :]:
                if g.tx or g.t_ns - f.t_ns > window_ns:
                    break
                cands.append(g)
            candidates[f.seq] = cands
    first_shapes: dict[str, Counter[str]] = defaultdict(Counter)
    counts: Counter[str] = Counter()
    for r in requests:
        if r.type_id is None:
            continue
        counts[r.type_id] += 1
        cands = candidates[r.seq]
        if cands:
            first_shapes[r.type_id][shape_of(cands[0])] += 1
    shape = {t: c.most_common(1)[0][0] for t, c in first_shapes.items()}
    responses: dict[int, Frame] = {}
    answered: Counter[str] = Counter()
    latency: dict[str, list[int]] = defaultdict(list)
    for r in requests:
        if r.type_id is None or r.type_id not in shape:
            continue
        for c in candidates[r.seq]:
            if shape_of(c) == shape[r.type_id]:
                responses[r.seq] = c
                answered[r.type_id] += 1
                latency[r.type_id].append(c.t_ns - r.t_ns)
                break
    rate = {t: answered[t] / counts[t] for t in counts}
    return Pairing(responses, rate, shape, dict(latency))
