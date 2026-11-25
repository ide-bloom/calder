"""Optional manual assist: match command tables in a PDF manual to inferred messages.

Extraction uses pdfplumber (install ``calder[manual]``). Lines and table rows
that start with an SCPI mnemonic, an IEEE 488.2 common command or a hex opcode
become manual entries; SCPI long forms are reduced to their short forms
(the upper-case part of each node) before matching.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from calder.runtime.protocol import normalize_header

_SCPI = re.compile(r"^\s*(\*[A-Z]{2,4}\??|:?[A-Za-z]+(?::[A-Za-z]+)*\??)(?:\s+<[^>]*>|\s+\{[^}]*\})*\s+[-–:]?\s*(.+)$")
_OPCODE = re.compile(r"^\s*(?:0x|\$)?([0-9A-Fa-f]{2})h?\s+[-–:]?\s*([A-Za-z].+)$")


@dataclass(frozen=True)
class ManualEntry:
    mnemonic: str
    key: str
    description: str
    page: int


def short_form(mnemonic: str) -> str:
    """SCPI short form: keep the upper-case prefix of each node ("SOURce" -> "SOUR")."""
    nodes = []
    for node in mnemonic.strip().lstrip(":").split(":"):
        query = node.endswith("?")
        core = node.rstrip("?")
        upper = "".join(ch for ch in core if ch.isupper() or ch.isdigit() or ch == "*")
        nodes.append((upper or core).upper() + ("?" if query else ""))
    return ":".join(nodes)


def parse_lines(lines: list[tuple[int, str]]) -> list[ManualEntry]:
    entries: list[ManualEntry] = []
    for page, line in lines:
        m = _SCPI.match(line)
        if m and (":" in m.group(1) or m.group(1).startswith("*") or m.group(1).endswith("?")):
            mnemonic = m.group(1)
            entries.append(ManualEntry(mnemonic, normalize_header(short_form(mnemonic)), m.group(2).strip(), page))
            continue
        m = _OPCODE.match(line)
        if m:
            entries.append(ManualEntry(f"0x{m.group(1).upper()}", m.group(1).lower(), m.group(2).strip(), page))
    return entries


def extract(path: str | Path) -> tuple[list[ManualEntry], int, str]:
    """Return (entries, page count, sha256)."""
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise RuntimeError("manual parsing needs pdfplumber: pip install 'calder[manual]'") from exc
    data = Path(path).read_bytes()
    lines: list[tuple[int, str]] = []
    pdf: Any
    with pdfplumber.open(str(path)) as pdf:
        pages = len(pdf.pages)
        for number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            lines.extend((number, ln) for ln in text.splitlines())
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [c.strip() for c in row if c]
                    if len(cells) >= 2:
                        lines.append((number, f"{cells[0]} {cells[-1]}"))
    return parse_lines(lines), pages, hashlib.sha256(data).hexdigest()


def match(entries: list[ManualEntry], key: str) -> ManualEntry | None:
    norm = normalize_header(key)
    for e in entries:
        if e.key == norm:
            return e
    base = norm.removesuffix("?")
    for e in entries:
        if e.key.removesuffix("?") == base:
            return e
    return None
