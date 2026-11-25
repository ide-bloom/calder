"""Field typing: encoding search and segmentation of variable regions.

Scoring (documented in docs/inference.md, "Field typing"):

* a UI-correlated interpretation scores 3.0 per varying byte, 0.1 per
  constant byte, plus its match rate;
* IEEE 754 values that are all finite, of plausible magnitude and short in
  decimal (what a person types) score 1.0 per byte (floats legitimately hold
  constant low-order bytes);
* multi-byte integers score 0.6 per varying byte when their most significant
  byte varies, but less than the least significant byte (the carry pattern of
  real counts), else 0.35; constant bytes inside them add 0.05;
* single-byte integers score 0.5, BCD 0.45 per varying byte, constant bytes 0.2.

Scoring varying bytes rather than width keeps zero padding out of numeric
fields: padding is explained more cheaply as constant bytes.
"""

from __future__ import annotations

import math
import statistics
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from calder.runtime.encodings import (
    Encoding,
    EncodingError,
    EncodingKind,
    Value,
    decode_binary,
    is_number,
    shortest_float32,
)

CorrelationFn = Callable[[Encoding, list[Value]], "Correlation | None"]


@dataclass(frozen=True)
class Correlation:
    element: str
    label: str
    match_rate: float
    support: int
    events: tuple[tuple[int, int], ...] = ()  # (session, ui event index)


@dataclass
class EncodingCandidate:
    encoding: Encoding
    score: float
    values: list[Value]
    reason: str
    correlation: Correlation | None = None


@dataclass
class Segment:
    offset: int
    length: int
    constant: bytes | None
    candidate: EncodingCandidate | None
    alternatives: list[EncodingCandidate] = field(default_factory=list)


def _is_short_decimal(value: float, digits: int) -> bool:
    if value == 0:
        return True
    if not math.isfinite(value):
        return False
    text = f"{value:.{digits}g}"
    return float(text) == value


def _plausible_float(value: float) -> bool:
    return value == 0 or (math.isfinite(value) and 1e-6 <= abs(value) <= 1e9)


def _float_candidates(spans: list[bytes]) -> list[EncodingCandidate]:
    width = len(spans[0])
    out: list[EncodingCandidate] = []
    if width not in (4, 8):
        return out
    for endian in ("le", "be"):
        enc = Encoding(EncodingKind.FLOAT, width=width, endian=endian)
        fmt = ("<" if endian == "le" else ">") + ("f" if width == 4 else "d")
        values: list[Value] = []
        ok = 0
        for s in spans:
            v = float(struct.unpack(fmt, s)[0])
            if width == 4 and math.isfinite(v):
                v = shortest_float32(v)
            values.append(v)
            if _plausible_float(v) and _is_short_decimal(v, 7 if width == 4 else 12):
                ok += 1
        frac = ok / len(spans)
        score = width * 1.0 * frac if frac == 1.0 else width * 0.2 * frac
        out.append(EncodingCandidate(enc, score, values, f"{ok}/{len(spans)} values are short finite decimals"))
    return out


def _signed(values: list[int], bits: int) -> bool:
    top = 1 << (bits - 1)
    high = [v for v in values if v & top]
    if not high:
        return False
    return all((v - (1 << bits)) > -(1 << (bits - 2)) for v in high)


def _varying(spans: list[bytes]) -> tuple[int, int]:
    """(varying, constant) column counts of a span across instances."""
    varying = sum(1 for i in range(len(spans[0])) if len({s[i] for s in spans}) > 1)
    return varying, len(spans[0]) - varying


def _int_candidates(spans: list[bytes]) -> list[EncodingCandidate]:
    width = len(spans[0])
    if width not in (1, 2, 4, 8):
        return []
    varying, constant = _varying(spans)
    out: list[EncodingCandidate] = []
    endians = ("le", "be") if width > 1 else ("le",)
    for endian in endians:
        raws = [int.from_bytes(s, "little" if endian == "le" else "big") for s in spans]
        signed = _signed(raws, width * 8)
        kind = EncodingKind.INT if signed else EncodingKind.UINT
        enc = Encoding(kind, width=width, endian=endian)
        values: list[Value] = [decode_binary(enc, s) for s in spans]  # type: ignore[misc]
        if width == 1:
            score, reason = 0.5, "single byte"
        else:
            msb = [s[-1] if endian == "le" else s[0] for s in spans]
            if signed:
                # Sign extension makes 0x00 and 0xFF neighbours; compare them as -1 and 0.
                msb = [b - 256 if b >= 128 else b for b in msb]
            lsb = [s[0] if endian == "le" else s[-1] for s in spans]
            msb_var = statistics.pvariance(msb) if len(spans) > 1 else 0.0
            lsb_var = statistics.pvariance(lsb) if len(spans) > 1 else 0.0
            carry = 0 < msb_var <= lsb_var
            score = varying * (0.6 if carry else 0.35) + constant * 0.05
            reason = "most significant byte varies least" if carry else "byte order without carry pattern"
        out.append(EncodingCandidate(enc, score, values, reason))
    if width in (1, 2, 4):
        for endian in endians:
            enc = Encoding(EncodingKind.BCD, width=width, endian=endian)
            try:
                bcd_values: list[Value] = [decode_binary(enc, s) for s in spans]  # type: ignore[misc]
            except EncodingError:
                continue
            out.append(
                EncodingCandidate(enc, varying * 0.45 + constant * 0.05, bcd_values, "all nibbles are decimal digits")
            )
    return out


def _with_scales(cand: EncodingCandidate, spans: list[bytes]) -> list[EncodingCandidate]:
    """Scaled-integer variants, only meaningful under UI correlation."""
    if cand.encoding.kind not in (EncodingKind.UINT, EncodingKind.INT, EncodingKind.BCD):
        return []
    out: list[EncodingCandidate] = []
    for exp in (-1, -2, -3, -4, 1, 2):
        enc = Encoding(cand.encoding.kind, width=cand.encoding.width, endian=cand.encoding.endian, scale_exp=exp)
        values: list[Value] = [decode_binary(enc, s) for s in spans]  # type: ignore[misc]
        out.append(EncodingCandidate(enc, cand.score * 0.9, values, f"{cand.reason}; scaled 10^{exp}"))
    return out


def binary_candidates(spans: list[bytes], correlate: CorrelationFn | None = None) -> list[EncodingCandidate]:
    """All interpretations of a byte span across instances, best first."""
    if not spans or any(len(s) != len(spans[0]) for s in spans):
        return []
    cands = _float_candidates(spans) + _int_candidates(spans)
    if correlate is not None:
        varying, constant = _varying(spans)
        scaled = [c2 for c in cands for c2 in _with_scales(c, spans)]
        for c in cands + scaled:
            hit = correlate(c.encoding, c.values)
            if hit is not None:
                c.correlation = hit
                c.score = varying * 3.0 + constant * 0.1 + hit.match_rate
                c.reason = f"matches UI '{hit.label or hit.element}' in {hit.support} instances"
        cands += [c for c in scaled if c.correlation is not None]
    cands.sort(key=lambda c: (-c.score, c.encoding.scale_exp != 0, c.encoding.kind.value, c.encoding.endian))
    return cands


def segment_region(
    frames: Sequence[bytes],
    start: int,
    end: int,
    correlate_at: Callable[[int, int], CorrelationFn | None] | None = None,
) -> list[Segment]:
    """Split ``[start, end)`` of equal-length frames into constant and typed fields."""
    if end <= start:
        return []
    columns_constant = [len({f[i] for f in frames}) == 1 for i in range(start, end)]
    n = end - start
    best: list[tuple[float, list[Segment]]] = [(-1e18, [])] * (n + 1)
    best[0] = (0.0, [])
    for i in range(n):
        base_score, base_segs = best[i]
        if base_score <= -1e17:
            continue
        if columns_constant[i]:
            seg = Segment(start + i, 1, bytes([frames[0][start + i]]), None)
            score = base_score + 0.2
            if score > best[i + 1][0]:
                best[i + 1] = (score, [*base_segs, seg])
        for w in (1, 2, 4, 8):
            if i + w > n or all(columns_constant[i : i + w]):
                continue
            spans = [bytes(f[start + i : start + i + w]) for f in frames]
            correlate = correlate_at(start + i, w) if correlate_at else None
            cands = binary_candidates(spans, correlate)
            if not cands:
                continue
            seg = Segment(start + i, w, None, cands[0], cands[1:4])
            score = base_score + cands[0].score
            if score > best[i + w][0]:
                best[i + w] = (score, [*base_segs, seg])
    segments = best[n][1]
    merged: list[Segment] = []
    for seg in segments:
        if (
            seg.constant is not None
            and merged
            and merged[-1].constant is not None
            and merged[-1].offset + merged[-1].length == seg.offset
        ):
            prev = merged[-1]
            assert prev.constant is not None
            merged[-1] = Segment(prev.offset, prev.length + seg.length, prev.constant + seg.constant, None)
        else:
            merged.append(seg)
    return merged


# ----- ASCII ---------------------------------------------------------------------


def _decimals(text: str) -> int:
    body = text.lower().split("e")[0]
    return len(body.split(".")[1]) if "." in body else 0


def ascii_number_encoding(tokens: list[str], *, fixed_width: bool = False) -> Encoding | None:
    """Encoding of numeric tokens, or None if any token is not a number."""
    stripped = [t.strip() for t in tokens]
    if not stripped or not all(is_number(t) for t in stripped):
        return None
    exponent = any("e" in t.lower() for t in stripped)
    has_point = any("." in t for t in stripped)
    plus_sign = any(t.startswith("+") for t in stripped) and all(t.startswith(("+", "-")) for t in stripped)
    widths = {len(t) for t in tokens}
    width = len(tokens[0]) if fixed_width and len(widths) == 1 else 0
    zero_pad = bool(width) and any(t.lstrip("+-").startswith("0") and len(t.lstrip("+-")) > 1 for t in stripped)
    if not has_point and not exponent:
        return Encoding(EncodingKind.ASCII_INT, width=width, plus_sign=plus_sign, zero_pad=zero_pad)
    places = {_decimals(t) for t in stripped}
    decimals = places.pop() if len(places) == 1 else None
    return Encoding(
        EncodingKind.ASCII_FLOAT,
        width=width,
        decimals=decimals,
        min_decimals=min(_decimals(t) for t in stripped),
        plus_sign=plus_sign,
        zero_pad=zero_pad,
        exponent=exponent,
    )


@dataclass
class TokenTyping:
    encoding: Encoding
    role: str  # value | enum | constant
    distinct: list[str]
    reason: str


def type_tokens(tokens: list[str], *, tx: bool, enum_limit: int = 12, fixed_width: bool = False) -> TokenTyping:
    distinct = sorted({t.strip() for t in tokens})
    number = ascii_number_encoding(tokens, fixed_width=fixed_width)
    if number is not None:
        if tx and len(distinct) == 1 and len(tokens) >= 3:
            return TokenTyping(number, "constant", distinct, "numeric token never varied")
        return TokenTyping(number, "value", distinct, f"{len(tokens)} numeric tokens")
    if all(len(t.strip()) >= 2 and t.strip()[0] == t.strip()[-1] and t.strip()[0] in "\"'" for t in tokens):
        return TokenTyping(Encoding(EncodingKind.ASCII_STRING), "value", distinct, "quoted string data")
    width = len(tokens[0]) if fixed_width and len({len(t) for t in tokens}) == 1 else 0
    enc = Encoding(EncodingKind.ASCII_ENUM, width=width)
    if len(distinct) == 1:
        role = "constant" if tx else "value"
        return TokenTyping(enc, role, distinct, "character data never varied")
    if len(distinct) <= enum_limit and len(tokens) >= len(distinct):
        return TokenTyping(enc, "enum", distinct, f"{len(distinct)} distinct character-data values")
    return TokenTyping(enc, "value", distinct, "free character data")
