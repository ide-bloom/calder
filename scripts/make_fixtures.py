"""Regenerate the small synthetic sessions in tests/fixtures/ (two per simulator).

Usage: python scripts/make_fixtures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.simulators.sessions import GENERATORS  # noqa: E402

from calder.session.io import save_session  # noqa: E402


def main() -> None:
    out = ROOT / "tests" / "fixtures"
    out.mkdir(parents=True, exist_ok=True)
    for kind, generate in GENERATORS.items():
        for seed in (1, 2):
            path = save_session(generate(seed), out / f"{kind}-{seed}.libsession")
            print(path.relative_to(ROOT), path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
