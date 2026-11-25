"""The committed fixture sessions stay readable and inferable (format compatibility)."""

from __future__ import annotations

from pathlib import Path

import pytest

from calder.infer.pipeline import infer
from calder.session.io import load_session

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("kind", ["thermostat", "pump", "modbus", "balance", "hid"])
def test_fixture_sessions_infer(kind: str):
    paths = sorted(FIXTURES.glob(f"{kind}-*.libsession"))
    assert len(paths) == 2
    model = infer([load_session(p) for p in paths])
    assert model.messages
    assert not [w for w in model.warnings if "does not decode" in w]
