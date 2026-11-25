"""Print the CHANGELOG.md section for a version (used for GitHub Release notes).

Usage: python scripts/changelog_section.py 0.1.0
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def section(text: str, version: str) -> str:
    pattern = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(text)
    if match is None:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    return match.group(1).strip() + "\n"


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.stdout.write(section((root / "CHANGELOG.md").read_text(encoding="utf-8"), sys.argv[1]))


if __name__ == "__main__":
    main()
