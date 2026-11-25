"""Command-sequence automata (k-tails) and precondition derivation.

k-tails (Biermann & Feldman, 1972): build a prefix tree acceptor from the
observed command sequences and merge states whose sets of futures of length up
to k are equal.

Preconditions: message P is a precondition of M when, in every session, every
occurrence of M is preceded by P with no "disabling" message D in between. D
is a disabler of P for M when it was observed to separate P from a later
re-sending of P that was then followed by M, and M never occurred between D
and the re-sent P. Positive-only traces cannot prove a precondition, so each
carries its support count and is presented as a hypothesis.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

END = "$"


@dataclass
class Automaton:
    k: int
    initial: int
    states: list[int]
    transitions: dict[tuple[int, int, str], int]  # (source, target, symbol) -> count
    accepting: set[int] = field(default_factory=set)

    def successors(self, state: int) -> dict[str, set[int]]:
        out: dict[str, set[int]] = defaultdict(set)
        for (s, t, sym), _ in self.transitions.items():
            if s == state:
                out[sym].add(t)
        return out

    def accepts(self, sequence: Sequence[str]) -> bool:
        current = {self.initial}
        for sym in sequence:
            nxt: set[int] = set()
            for s in current:
                nxt |= self.successors(s).get(sym, set())
            if not nxt:
                return False
            current = nxt
        return bool(current & self.accepting)


def k_tails(sequences: Sequence[Sequence[str]], k: int = 2) -> Automaton:
    # Prefix tree acceptor.
    children: list[dict[str, int]] = [{}]
    counts: Counter[tuple[int, str]] = Counter()
    ends: set[int] = set()
    for seq in sequences:
        node = 0
        for sym in seq:
            counts[(node, sym)] += 1
            nxt = children[node].get(sym)
            if nxt is None:
                nxt = len(children)
                children.append({})
                children[node][sym] = nxt
            node = nxt
        ends.add(node)

    def tails(node: int, depth: int) -> frozenset[tuple[str, ...]]:
        out: set[tuple[str, ...]] = set()
        if node in ends:
            out.add((END,))
        if depth == 0:
            return frozenset(out | {()})
        for sym, child in children[node].items():
            for t in tails(child, depth - 1):
                out.add((sym, *t))
        if not children[node] and node not in ends:
            out.add(())
        return frozenset(out)

    signature: dict[frozenset[tuple[str, ...]], int] = {}
    state_of: list[int] = []
    for node in range(len(children)):
        sig = tails(node, k)
        if sig not in signature:
            signature[sig] = len(signature)
        state_of.append(signature[sig])
    transitions: Counter[tuple[int, int, str]] = Counter()
    for node, kids in enumerate(children):
        for sym, child in kids.items():
            transitions[(state_of[node], state_of[child], sym)] += counts[(node, sym)]
    accepting = {state_of[n] for n in ends}
    return Automaton(k, state_of[0], sorted(set(state_of)), dict(transitions), accepting)


@dataclass(frozen=True)
class PreconditionHit:
    message: str
    requires: str
    disabled_by: tuple[str, ...]
    support: int
    sessions: int
    events: tuple[tuple[int, int], ...]  # (session, position) of supporting occurrences


def _disablers(sequences: Sequence[Sequence[str]], target: str, req: str, symbols: set[str]) -> list[str]:
    candidates: list[str] = []
    for d in sorted(symbols - {target, req}):
        observed_break = False
        violated = False
        for seq in sequences:
            seen_req = False
            for i, sym in enumerate(seq):
                if sym == req:
                    seen_req = True
                elif sym == d and seen_req:
                    # After D, the next relevant symbol must be P (re-enable), not M.
                    for later in seq[i + 1 :]:
                        if later == target:
                            violated = True
                            break
                        if later == req:
                            if target in seq[seq.index(later, i + 1) :]:
                                observed_break = True
                            break
                    if violated:
                        break
            if violated:
                break
        if observed_break and not violated:
            candidates.append(d)
    return candidates


def preconditions(
    sequences: Sequence[Sequence[str]],
    *,
    min_support: int = 2,
    requirements: set[str] | None = None,
    targets: set[str] | None = None,
) -> list[PreconditionHit]:
    """Derive preconditions. ``requirements`` limits which messages may enable
    others (read-only queries cannot change instrument state, so callers pass
    only state-changing messages); ``targets`` limits which messages are checked.
    """
    symbols = {s for seq in sequences for s in seq}
    hits: list[PreconditionHit] = []
    allowed = symbols if requirements is None else symbols & requirements
    for target in sorted(symbols if targets is None else symbols & targets):
        occurrences = [(si, i) for si, seq in enumerate(sequences) for i, s in enumerate(seq) if s == target]
        if len(occurrences) < min_support:
            continue
        for req in sorted(allowed - {target}):
            ok = True
            for si, i in occurrences:
                if req not in sequences[si][:i]:
                    ok = False
                    break
            if not ok:
                continue
            disablers = _disablers(sequences, target, req, allowed)
            if disablers:
                # With disablers, every occurrence must have P after the last D.
                for si, i in occurrences:
                    prefix = sequences[si][:i]
                    last_req = max(j for j, s in enumerate(prefix) if s == req)
                    if any(s in disablers for s in prefix[last_req + 1 :]):
                        ok = False
                        break
            if not ok:
                continue
            hits.append(
                PreconditionHit(
                    target,
                    req,
                    tuple(disablers),
                    len(occurrences),
                    len({si for si, _ in occurrences}),
                    tuple(occurrences[:20]),
                )
            )
    return _drop_batched(hits, sequences)


def _drop_batched(hits: list[PreconditionHit], sequences: Sequence[Sequence[str]]) -> list[PreconditionHit]:
    """Drop requirements that are the vendor software's batching, not instrument state.

    When P immediately precedes M in at least 90% of M's occurrences, the two are
    sent together for one user action (e.g. "setpoint" then "rate" on Apply); the
    ordering says nothing about what the instrument requires.
    """
    out: list[PreconditionHit] = []
    for h in hits:
        adjacent = total = 0
        for seq in sequences:
            for i, sym in enumerate(seq):
                if sym == h.message:
                    total += 1
                    adjacent += i > 0 and seq[i - 1] == h.requires
        if total and adjacent / total >= 0.9:
            continue
        out.append(h)
    return out
