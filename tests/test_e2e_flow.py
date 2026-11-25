"""record -> infer -> generate -> test -> verify for every simulator, through the CLI.

Sessions come from the programmatic recorder (tests/simulators/sessions.py); the
capture tests in tests/capture/ cover recording real applications.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from calder.cli import app
from calder.session.io import save_session
from tests.conftest import sessions_for
from tests.simulators.balance import Balance
from tests.simulators.base import Instrument, serve_tcp
from tests.simulators.hid_stirrer import Stirrer
from tests.simulators.modbus_tc import ModbusController
from tests.simulators.pump import SyringePump
from tests.simulators.thermostat import Thermostat

SIMULATORS: dict[str, type[Instrument]] = {
    "thermostat": Thermostat,
    "pump": SyringePump,
    "modbus": ModbusController,
    "balance": Balance,
    "hid": Stirrer,
}


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", *args], cwd=cwd, capture_output=True, text=True, timeout=600)


@pytest.mark.slow
def test_full_flow(kind: str, tmp_path: Path):
    runner = CliRunner()
    paths = [str(save_session(s, tmp_path / "sessions" / f"{i}.libsession")) for i, s in enumerate(sessions_for(kind))]
    model = tmp_path / "model.json"
    result = runner.invoke(app, ["infer", *paths, "--out", str(model), "--name", kind])
    assert result.exit_code == 0, result.output
    out = tmp_path / "driver"
    result = runner.invoke(app, ["generate", str(model), "--out", str(out)])
    assert result.exit_code == 0, result.output

    lint = run(["ruff", "check", "."], out)
    assert lint.returncode == 0, lint.stdout + lint.stderr
    types = run(["mypy"], out)
    assert types.returncode == 0, types.stdout + types.stderr
    tests = run(["pytest", "-q", "-p", "no:cacheprovider"], out)
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert " passed" in tests.stdout

    with serve_tcp(SIMULATORS[kind]()) as server:
        result = runner.invoke(
            app,
            [
                "verify",
                str(out),
                "--resource",
                f"tcp://{server.address}",
                "--phase",
                "read",
                "--rate",
                "20",
                "--log",
                str(tmp_path / "verify.jsonl"),
            ],
        )
    assert result.exit_code == 0, result.output
    assert "exchanges matched" in result.output
    assert (tmp_path / "verify.jsonl").read_text(encoding="utf-8").count('"dir": "tx"') >= 1
