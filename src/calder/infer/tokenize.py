"""ASCII tokenization with SCPI / IEEE 488.2 awareness and generic fallback rules."""

from __future__ import annotations

import re
from dataclasses import dataclass

from calder.runtime.protocol import normalize_header, split_args

# IEEE 488.2 common commands (section 10 of the standard).
IEEE488_COMMON: dict[str, str] = {
    "*CLS": "Clear status",
    "*ESE": "Standard event status enable",
    "*ESE?": "Standard event status enable query",
    "*ESR?": "Standard event status register query",
    "*IDN?": "Identification query",
    "*OPC": "Operation complete",
    "*OPC?": "Operation complete query",
    "*OPT?": "Option identification query",
    "*PSC": "Power-on status clear",
    "*PSC?": "Power-on status clear query",
    "*RCL": "Recall instrument state",
    "*RST": "Reset",
    "*SAV": "Save instrument state",
    "*SRE": "Service request enable",
    "*SRE?": "Service request enable query",
    "*STB?": "Read status byte query",
    "*TRG": "Trigger",
    "*TST?": "Self-test query",
    "*WAI": "Wait to continue",
}

_SCPI_HEADER = re.compile(r"^(\*[A-Za-z]{2,4}\??|:?[A-Za-z][A-Za-z0-9_]*(?::[A-Za-z][A-Za-z0-9_]*)*\??)$")
_GENERIC_HEADER = re.compile(r"^[A-Za-z*?#@!$%&_:/]+")


@dataclass(frozen=True)
class Tokens:
    text: str
    header: str
    header_sep: str
    args: tuple[str, ...]

    @property
    def key(self) -> str:
        return normalize_header(self.header)

    @property
    def is_query(self) -> bool:
        return self.header.endswith("?")


def scpi_tokens(text: str) -> Tokens | None:
    stripped = text.strip()
    if not stripped:
        return None
    head, sep, rest = stripped.partition(" ")
    if not _SCPI_HEADER.match(head):
        return None
    if ";" in rest or ";" in head:
        return None
    args = tuple(split_args(rest.strip(), ",")) if sep else ()
    return Tokens(stripped, head, " ", args)


def is_scpi_like(texts: list[str], threshold: float = 0.9) -> tuple[bool, float]:
    """SCPI if most commands parse and some use hierarchy, queries or common commands."""
    if not texts:
        return False, 0.0
    parsed = [scpi_tokens(t) for t in texts]
    ok = [p for p in parsed if p is not None]
    ratio = len(ok) / len(texts)
    hierarchical = any(":" in p.header or p.header.startswith("*") or p.is_query for p in ok)
    return ratio >= threshold and hierarchical, ratio


def generic_tokens(text: str, arg_sep: str | None = None) -> Tokens:
    stripped = text.strip()
    match = _GENERIC_HEADER.match(stripped)
    header = match.group(0) if match else ""
    rest = stripped[len(header) :]
    header_sep = ""
    if rest.startswith(" "):
        header_sep = " "
        rest = rest.strip()
    if arg_sep is None:
        arg_sep = "," if "," in rest else " " if " " in rest.strip() else ","
    args = tuple(a for a in split_args(rest, arg_sep)) if rest else ()
    if arg_sep == " ":
        args = tuple(a for a in args if a)
    return Tokens(stripped, header, header_sep, args)


# ----- fixed-width shapes ----------------------------------------------------------

_SHAPE_TOKEN = re.compile(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[A-Za-z]+|\s+|.", re.DOTALL)


def char_class(token: str) -> str:
    if token[0].isspace():
        return "s"
    if token[0].isalpha():
        return "a"
    if token[0].isdigit() or (token[0] in "+-" and len(token) > 1):
        return "n"
    return "p"


def shape_tokens(text: str) -> list[tuple[int, str, str]]:
    """Split into (column, class, text) tokens: numbers, words, spaces, punctuation."""
    return [(m.start(), char_class(m.group(0)), m.group(0)) for m in _SHAPE_TOKEN.finditer(text)]


def shape_signature(text: str) -> str:
    """Token classes with positions; equal signatures mean equal fixed-width layout."""
    parts = []
    for col, cls, tok in shape_tokens(text):
        parts.append(f"{cls}{col}:{len(tok)}" if cls != "p" else f"p{col}:{tok}")
    return f"{len(text)}|" + ",".join(parts)


def loose_signature(text: str) -> str:
    """Token classes only; used to group frames whose widths vary."""
    return ",".join(cls if cls != "p" else tok for _, cls, tok in shape_tokens(text))
