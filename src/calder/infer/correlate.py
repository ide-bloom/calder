"""Correlating wire fields with what the user did in the vendor UI.

* Values: the user typed or selected a value shortly before a command (or a
  display showed a value shortly after a response). A field interpretation
  correlates with a UI element when it decodes to exactly that value in at
  least 90% of instances (and at least two).
* Enumerations: field values that map one-to-one onto UI choices.
* Bindings: buttons whose clicks are followed by a message type far more often
  than chance (lift over a time-uniform baseline).
"""

from __future__ import annotations

import bisect
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from calder.infer.fields import Correlation, CorrelationFn
from calder.infer.streams import Frame
from calder.runtime.encodings import Encoding, Value, values_equal
from calder.session.models import Session, UiEvent, UiKind

VALUE_WINDOW_NS = 30_000_000_000
DISPLAY_WINDOW_NS = 3_000_000_000
BIND_WINDOW_NS = 500_000_000
# UI Automation delivers events asynchronously: a click or selection can be timestamped
# after the command it caused has already been written. Allow UI events up to this much
# later than the wire event they explain.
UI_LATENCY_NS = 500_000_000
MIN_MATCH_RATE = 0.9
MIN_SUPPORT = 2

_UNIT_PATTERNS = (
    re.compile(r"^(?P<name>.*?)\s*\((?P<unit>[^()]+)\)\s*:?\s*$"),
    re.compile(r"^(?P<name>.*?)\s*\[(?P<unit>[^\[\]]+)\]\s*:?\s*$"),
    re.compile(r"^(?P<name>.*?)\s+in\s+(?P<unit>[^\s]+)\s*:?\s*$", re.IGNORECASE),
)
_NUMBER = re.compile(r"[+-]?(?:\d+[.,]?\d*|[.,]\d+)(?:[eE][+-]?\d+)?")


def parse_label(label: str) -> tuple[str, str | None]:
    """Split "Setpoint (°C)" into ("Setpoint", "°C")."""
    text = label.strip().rstrip(":").strip()
    for pattern in _UNIT_PATTERNS:
        m = pattern.match(text)
        if m and m.group("name").strip():
            return m.group("name").strip(), m.group("unit").strip()
    return text, None


def snake(text: str) -> str:
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", text)
    name = "_".join(w.lower() for w in words) or "value"
    if name[0].isdigit():
        name = f"v_{name}"
    return name


def parse_number(text: str | None) -> float | None:
    if text is None:
        return None
    m = _NUMBER.search(text.strip())
    if not m:
        return None
    raw = m.group(0)
    if "," in raw and "." not in raw:
        raw = raw.replace(",", ".")
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


@dataclass(frozen=True)
class UiValue:
    session: int
    index: int
    t_ns: int
    element: str
    label: str
    name: str
    kind: UiKind
    value: str


class UiContext:
    """Per-session UI timelines with fast "latest value before t" lookups."""

    def __init__(self, sessions: Sequence[Session]) -> None:
        self.values: dict[int, dict[str, list[UiValue]]] = defaultdict(lambda: defaultdict(list))
        self.invokes: dict[int, list[UiEvent]] = defaultdict(list)
        self.labels: dict[str, str] = {}
        self.names: dict[str, str] = {}
        for si, s in enumerate(sessions):
            for e in s.ui_events:
                key = e.element.key
                if not key:
                    continue
                self.labels.setdefault(key, e.element.label or e.element.name)
                self.names.setdefault(key, e.element.name or e.element.automation_id)
                if e.kind is UiKind.INVOKE:
                    self.invokes[si].append(e)
                elif e.kind in (UiKind.VALUE, UiKind.SELECTION, UiKind.TOGGLE) and e.value is not None:
                    self.values[si][key].append(
                        UiValue(si, e.index, e.t_ns, key, e.element.label, e.element.name, e.kind, e.value)
                    )
        self._times: dict[tuple[int, str], list[int]] = {}
        for si, by_key in self.values.items():
            for key, vals in by_key.items():
                vals.sort(key=lambda v: v.t_ns)
                self._times[(si, key)] = [v.t_ns for v in vals]

    @property
    def empty(self) -> bool:
        return not any(self.values.values()) and not any(self.invokes.values())

    def elements(self) -> list[str]:
        keys: set[str] = set()
        for by_key in self.values.values():
            keys.update(by_key)
        return sorted(keys)

    def before(self, session: int, key: str, t_ns: int, window: int = VALUE_WINDOW_NS) -> UiValue | None:
        times = self._times.get((session, key))
        if not times:
            return None
        i = bisect.bisect_right(times, t_ns) - 1
        if i < 0:
            return None
        v = self.values[session][key][i]
        return v if t_ns - v.t_ns <= window else None

    def after(self, session: int, key: str, t_ns: int, window: int = DISPLAY_WINDOW_NS) -> UiValue | None:
        times = self._times.get((session, key))
        if not times:
            return None
        i = bisect.bisect_left(times, t_ns)
        if i >= len(times):
            return None
        v = self.values[session][key][i]
        return v if v.t_ns - t_ns <= window else None

    def context(self, frame: Frame, *, after: bool = False) -> dict[str, UiValue]:
        out: dict[str, UiValue] = {}
        for key in self.values.get(frame.session, {}):
            if after:
                v = self.after(frame.session, key, frame.t_ns)
            else:
                v = self.before(frame.session, key, frame.t_ns + UI_LATENCY_NS)
            if v is not None:
                out[key] = v
        return out


def correlate_values(
    ui: UiContext, frames: Sequence[Frame], values: Sequence[Value], *, after: bool = False
) -> Correlation | None:
    """Best UI element whose numeric values equal the decoded field values."""
    if ui.empty or not frames:
        return None
    best: Correlation | None = None
    contexts = [ui.context(f, after=after) for f in frames]
    for key in ui.elements():
        support = matches = 0
        matched: set[float] = set()
        events: list[tuple[int, int]] = []
        for ctx, value in zip(contexts, values, strict=True):
            v = ctx.get(key)
            if v is None:
                continue
            number = parse_number(v.value)
            if number is None or isinstance(value, str | bytes | bool):
                continue
            support += 1
            if values_equal(float(value), number):
                matches += 1
                matched.add(round(number, 9))
                events.append((v.session, v.index))
        # Agreement on a single repeated value (often zero) is no evidence.
        if support < MIN_SUPPORT or len(matched) < 2:
            continue
        distinct = {round(float(x), 9) for x in values if isinstance(x, int | float)}
        if len(distinct) < 2 and len(values) > 2:
            continue
        rate = matches / support
        if rate >= MIN_MATCH_RATE and (best is None or (rate, support) > (best.match_rate, best.support)):
            best = Correlation(key, ui.labels.get(key, key), rate, support, tuple(events[:20]))
    return best


def correlator(ui: UiContext, frames: Sequence[Frame], *, after: bool = False) -> CorrelationFn:
    """Bind frames for use as a :data:`calder.infer.fields.CorrelationFn`."""

    def fn(_encoding: Encoding, values: list[Value]) -> Correlation | None:
        return correlate_values(ui, frames, values, after=after)

    return fn


@dataclass(frozen=True)
class EnumMapping:
    element: str
    label: str
    mapping: dict[str, str]  # wire value -> UI choice
    support: int


def correlate_enum(ui: UiContext, frames: Sequence[Frame], wire_values: Sequence[str]) -> EnumMapping | None:
    """A UI selection whose choices map one-to-one onto the wire values."""
    if ui.empty:
        return None
    best: EnumMapping | None = None
    contexts = [ui.context(f) for f in frames]
    for key in ui.elements():
        pairs: list[tuple[str, str]] = []
        for ctx, wire in zip(contexts, wire_values, strict=True):
            v = ctx.get(key)
            if v is None or v.kind not in (UiKind.SELECTION, UiKind.TOGGLE):
                continue
            pairs.append((wire, v.value))
        if len(pairs) < MIN_SUPPORT:
            continue
        forward: dict[str, Counter[str]] = defaultdict(Counter)
        backward: dict[str, Counter[str]] = defaultdict(Counter)
        for wire, choice in pairs:
            forward[wire][choice] += 1
            backward[choice][wire] += 1
        consistent = sum(c.most_common(1)[0][1] for c in forward.values())
        if consistent / len(pairs) < MIN_MATCH_RATE or len(forward) < 2:
            continue
        mapping = {wire: c.most_common(1)[0][0] for wire, c in forward.items()}
        if len(set(mapping.values())) != len(mapping):
            continue
        if best is None or len(pairs) > best.support:
            best = EnumMapping(key, ui.labels.get(key, key), mapping, len(pairs))
    return best


@dataclass(frozen=True)
class Binding:
    element: str
    name: str
    type_id: str
    support: int
    clicks: int
    lift: float
    instances: tuple[int, ...]  # frame seq numbers bound to the click


def invoke_bindings(ui: UiContext, frames: Sequence[Frame], sessions: Sequence[Session]) -> list[Binding]:
    """Message types that follow a button click far more often than chance."""
    by_session: dict[int, list[Frame]] = defaultdict(list)
    for f in frames:
        if f.tx and f.type_id is not None:
            by_session[f.session].append(f)
    for fl in by_session.values():
        fl.sort(key=lambda f: f.t_ns)
    clicks: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for si, events in ui.invokes.items():
        for e in events:
            clicks[e.element.key].append((si, e.t_ns))
    if not clicks:
        return []
    all_clicks = sorted((si, t) for v in clicks.values() for si, t in v)
    type_ids = sorted({f.type_id for f in frames if f.tx and f.type_id})

    index: dict[tuple[int, str], tuple[list[int], list[Frame]]] = {}
    for si, fl in by_session.items():
        per_type: dict[str, list[Frame]] = defaultdict(list)
        for f in fl:
            assert f.type_id is not None
            per_type[f.type_id].append(f)
        for type_id, tf in per_type.items():
            index[(si, type_id)] = ([f.t_ns for f in tf], tf)

    def follows(si: int, t: int, type_id: str, until: int) -> Frame | None:
        entry = index.get((si, type_id))
        if entry is None:
            return None
        times, tf = entry
        i = bisect.bisect_left(times, t - UI_LATENCY_NS)
        if i < len(times) and times[i] <= min(t + BIND_WINDOW_NS, until):
            return tf[i]
        return None

    # Baseline: probability that a type appears in a window starting at a random time.
    baseline: dict[str, float] = {}
    for type_id in type_ids:
        hits = probes = 0
        for si in range(len(sessions)):
            times = [f.t_ns for f in by_session.get(si, [])]
            if not times:
                continue
            lo, hi = times[0], times[-1]
            steps = 100
            for k in range(steps):
                t = lo + (hi - lo) * k // steps
                probes += 1
                hits += follows(si, t, type_id, t + BIND_WINDOW_NS) is not None
        baseline[type_id] = hits / probes if probes else 0.0
    out: list[Binding] = []
    for key, click_times in clicks.items():
        for type_id in type_ids:
            bound: list[int] = []
            for si, t in click_times:
                later = [c for c in all_clicks if c[0] == si and c[1] > t]
                until = later[0][1] if later else t + BIND_WINDOW_NS
                hit = follows(si, t, type_id, until)
                if hit is not None:
                    bound.append(hit.seq)
            if len(bound) < MIN_SUPPORT or len(bound) / len(click_times) < 0.8:
                continue
            lift = (len(bound) / len(click_times)) / max(baseline.get(type_id, 0.0), 0.02)
            if lift < 3.0:
                continue
            out.append(Binding(key, ui.names.get(key, key), type_id, len(bound), len(click_times), lift, tuple(bound)))
    return out
