"""Checksum search.

Search space (see docs/inference.md):

* every catalogued CRC (CRC RevEng catalogue, widths 8/16/32);
* the parametrized space spanned by the catalogued polynomials of each width
  with initial value {0, all-ones, catalogued inits}, reflection {in=out}, and
  final XOR {0, all-ones};
* additive sums, two's- and ones'-complement sums, XOR/LRC and Fletcher-16;

over coverage ranges starting at bytes 0..3 and ending at the checksum, with
the checksum stored at the frame end or just before a one-byte trailer, raw
(both byte orders) or as ASCII hex. A candidate is accepted only if it
validates every frame.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from calder.runtime.checksums import (
    CRC_CATALOGUE,
    SIMPLE_SUMS,
    ChecksumSpec,
    CrcParams,
    crc,
)


@dataclass(frozen=True)
class Algorithm:
    name: str
    width: int
    func: Callable[[bytes], int]
    params: CrcParams | None
    rank: int  # 0 = catalogued CRC, 1 = simple sum, 2 = parametrized CRC


def _crc_func(p: CrcParams) -> Callable[[bytes], int]:
    return lambda data: crc(p, data)


def algorithms(widths: tuple[int, ...] = (8, 16, 32)) -> list[Algorithm]:
    algs: list[Algorithm] = []
    seen: set[tuple[int, int, int, bool, int]] = set()
    for p in CRC_CATALOGUE:
        if p.width in widths:
            algs.append(Algorithm(p.name, p.width, _crc_func(p), p, 0))
            seen.add((p.width, p.poly, p.init, p.refin, p.xorout))
    for name, (width, func) in SIMPLE_SUMS.items():
        if width in widths:
            algs.append(Algorithm(name, width, func, None, 1))
    for width in widths:
        mask = (1 << width) - 1
        polys = sorted({p.poly for p in CRC_CATALOGUE if p.width == width})
        for poly in polys:
            inits = {0, mask} | {p.init for p in CRC_CATALOGUE if p.width == width and p.poly == poly}
            for init in sorted(inits):
                for ref in (False, True):
                    for xorout in (0, mask):
                        key = (width, poly, init, ref, xorout)
                        if key in seen:
                            continue
                        seen.add(key)
                        name = (
                            f"CRC-{width}/CUSTOM(poly=0x{poly:0{width // 4}X},init=0x{init:0{width // 4}X},"
                            f"ref={'true' if ref else 'false'},xorout=0x{xorout:0{width // 4}X})"
                        )
                        params = CrcParams(name, width, poly, init, ref, ref, xorout)
                        algs.append(Algorithm(name, width, _crc_func(params), params, 2))
    return algs


_ALGORITHMS: list[Algorithm] | None = None


def all_algorithms() -> list[Algorithm]:
    global _ALGORITHMS  # noqa: PLW0603 - module-level cache of a pure computation
    if _ALGORITHMS is None:
        _ALGORITHMS = algorithms()
    return _ALGORITHMS


@dataclass(frozen=True)
class ChecksumCandidate:
    spec: ChecksumSpec
    rank: int
    frames: int

    def describe(self) -> str:
        return self.spec.describe()


def _layouts(min_len: int, *, ascii_hex: bool) -> Iterator[tuple[int, int, int, str]]:
    """Yield (nbytes, position, start, encoding) placements to test."""
    encodings = ("raw", "ascii_hex") if ascii_hex else ("raw",)
    for encoding in encodings:
        for width_bytes in (1, 2, 4):
            nbytes = width_bytes * (2 if encoding == "ascii_hex" else 1)
            for trailer in (0, 1):
                position = -(nbytes + trailer)
                for start in range(0, 4):
                    if min_len + position - start < 1:
                        continue
                    yield width_bytes, position, start, encoding


def _target(frame: bytes, position: int, width_bytes: int, encoding: str) -> tuple[int, int] | None:
    begin = len(frame) + position
    if encoding == "ascii_hex":
        text = frame[begin : begin + width_bytes * 2]
        try:
            value = int(text.decode("ascii"), 16)
        except (UnicodeDecodeError, ValueError):
            return None
        if text.upper() != text:
            return None
        return value, value
    raw = frame[begin : begin + width_bytes]
    return int.from_bytes(raw, "big"), int.from_bytes(raw, "little")


def search(frames: list[bytes], *, ascii_hex: bool = False, limit: int = 200) -> list[ChecksumCandidate]:
    """Return every checksum that validates all frames, best first."""
    sample = [f for f in frames if f][:limit]
    if len(sample) < 2 or len(set(sample)) < 2:
        return []
    min_len = min(len(f) for f in sample)
    found: list[ChecksumCandidate] = []
    algs = all_algorithms()
    first = sample[0]
    for width_bytes, position, start, encoding in _layouts(min_len, ascii_hex=ascii_hex):
        if any(_target(f, position, width_bytes, encoding) is None for f in sample):
            continue
        t0 = _target(first, position, width_bytes, encoding)
        assert t0 is not None
        data0 = first[start : len(first) + position]
        for alg in algs:
            if alg.width != width_bytes * 8:
                continue
            value = alg.func(data0)
            endians = [e for e, t in (("be", t0[0]), ("le", t0[1])) if t == value]
            if width_bytes == 1 or encoding == "ascii_hex":
                endians = endians[:1]
            for endian in endians:
                ok = True
                for f in sample[1:]:
                    t = _target(f, position, width_bytes, encoding)
                    assert t is not None
                    expected = t[0] if endian == "be" else t[1]
                    if alg.func(f[start : len(f) + position]) != expected:
                        ok = False
                        break
                if not ok:
                    continue
                p = alg.params
                spec = ChecksumSpec(
                    algorithm=alg.name,
                    width=alg.width,
                    start=start,
                    end=position,
                    position=position,
                    endian=endian,
                    encoding=encoding,
                    poly=p.poly if p else None,
                    init=p.init if p else None,
                    refin=p.refin if p else None,
                    refout=p.refout if p else None,
                    xorout=p.xorout if p else None,
                )
                found.append(ChecksumCandidate(spec, alg.rank, len(sample)))
    # Prefer catalogued CRCs, wider checks, checksums at the very end, then the
    # widest coverage (a start byte excluded from coverage is common, so start=0 and
    # start=1 tie only when the start byte is zero).
    found.sort(key=lambda c: (c.rank, -c.spec.width, -c.spec.position, c.spec.start, c.spec.endian != "be"))
    return found
