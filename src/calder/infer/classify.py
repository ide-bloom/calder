"""Classify commands as read-only, configuring or actuating.

Classification decides what the generated MCP server exposes freely and what
``verify`` sends in which phase, so it errs toward the more dangerous class:
any actuating cue wins. The user confirms or changes it in the review UI.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

ACTUATING_WORDS = frozenset(
    {
        "start",
        "run",
        "go",
        "move",
        "home",
        "jog",
        "heat",
        "cool",
        "dispense",
        "aspirate",
        "inject",
        "infuse",
        "withdraw",
        "pump",
        "fire",
        "trigger",
        "trg",
        "stir",
        "spin",
        "shake",
        "open",
        "close",
        "valve",
        "output",
        "outp",
        "enable",
        "energize",
        "pressurize",
        "vent",
        "purge",
        "stop",
        "abort",
        "halt",
        "pause",
        "resume",
        "arm",
        "motor",
        "lamp",
        "laser",
        "init",
        "initialize",
        "prime",
        "flush",
        "zero_move",
        "apply_voltage",
        "hv",
    }
)
CONFIGURING_WORDS = frozenset(
    {
        "set",
        "config",
        "conf",
        "configure",
        "mode",
        "rate",
        "setpoint",
        "range",
        "unit",
        "units",
        "select",
        "zero",
        "tare",
        "reset",
        "rst",
        "cls",
        "calibrate",
        "cal",
        "store",
        "save",
        "rem",
        "remote",
        "loc",
        "local",
        "write",
        "sour",
        "source",
        "direction",
        "speed",
        "volume",
    }
)


def words(*texts: str) -> set[str]:
    out: set[str] = set()
    for text in texts:
        for w in re.split(r"[^A-Za-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text)):
            if w:
                out.add(w.lower())
    return out


def classify(
    *,
    is_query: bool,
    has_inputs: bool,
    texts: Iterable[str],
    ui_texts: Iterable[str] = (),
) -> tuple[str, str]:
    """Return (classification, reason)."""
    found = words(*texts)
    ui_found = words(*ui_texts)
    act = sorted((found | ui_found) & ACTUATING_WORDS)
    if is_query and not has_inputs and not (ui_found & ACTUATING_WORDS):
        return "read_only", "query without parameters"
    if act:
        source = "UI control" if ui_found & ACTUATING_WORDS else "command naming"
        return "actuating", f"{source} contains {', '.join(act)}"
    if is_query and not has_inputs:
        return "read_only", "query without parameters"
    conf = sorted((found | ui_found) & CONFIGURING_WORDS)
    if conf:
        return "configuring", f"naming contains {', '.join(conf)}"
    if has_inputs:
        return "configuring", "command carries parameters"
    if is_query:
        return "read_only", "query"
    return "configuring", "command without response (changes state)"
