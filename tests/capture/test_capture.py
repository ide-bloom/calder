"""Capture tests on Windows: the DLL in both bitnesses, the WPF test vendor app with UI
correlation, and the full record -> infer -> generate -> test -> verify flow.

Requires `python scripts/build_native.py` (DLLs, launchers, hook test, UI helper, vendor app).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from tests.conftest import capture_ready, native_dir, vendorapp
from tests.simulators.base import PipeServer, serve_tcp
from tests.simulators.pump import SyringePump
from tests.simulators.thermostat import Thermostat
from typer.testing import CliRunner

from calder.capture.filters import CaptureFilter
from calder.capture.recorder import RecordOptions, record
from calder.cli import app
from calder.infer.pipeline import InferOptions, infer
from calder.model.io import save_model
from calder.model.schema import FieldRole
from calder.session.io import load_session, save_session

pytestmark = pytest.mark.capture
HERE = Path(__file__).parent
ARCHES = ["x64", "x86"]


def _require(arch: str, *, app: bool = False) -> None:
    reason = capture_ready(arch)
    if reason is None and app and not vendorapp(arch).is_file():
        reason = f"WPF test vendor app not built: {vendorapp(arch)}"
    if reason is None and app and not (native_dir("x64") / "calder-uia.exe").is_file():
        reason = "UI Automation helper calder-uia.exe not built"
    if reason:
        if os.environ.get("CALDER_REQUIRE_CAPTURE") == "1":
            pytest.fail(reason)
        pytest.skip(reason)


@pytest.mark.parametrize("arch", ARCHES)
def test_dll_records_every_transport(arch: str):
    _require(arch)
    bits = "64" if arch == "x64" else "32"
    name = f"calder-hook-{uuid.uuid4().hex[:8]}"
    with PipeServer(Thermostat(), name) as _pipe, serve_tcp(Thermostat()) as tcp:
        session = record(
            RecordOptions(app=str(native_dir(arch) / f"calder_hooktest{bits}.exe"), args=[name, str(tcp.port)])
        )
    kinds = {(e.transport.value, e.kind.value, e.direction.value) for e in session.events}
    for transport in ("serial", "tcp", "visa", "ftdi"):
        assert (transport, "data", "tx") in kinds, transport
        assert (transport, "data", "rx") in kinds, transport
    assert ("ftdi", "config", "none") in kinds
    assert ("visa", "config", "none") in kinds
    assert ("winusb", "error", "none") in kinds
    assert ("hid", "error", "none") in kinds
    formatted = [e.data for e in session.events if e.meta.get("api") == "viPrintf"]
    assert formatted == [b"SOUR:VOLT 1.50\n"]
    assert session.meta.dropped_events == 0
    attached = [i for i in session.meta.extra["dll"] if i.get("event") == "attached"]
    assert attached and attached[0]["bitness"] == int(bits)


def _record_app(arch: str, protocol: str, tmp_path: Path) -> Path:
    if protocol == "thermostat":
        name = f"calder-wpf-{uuid.uuid4().hex[:8]}"
        server = PipeServer(Thermostat(), name).start()
        args = ["--protocol", "thermostat", "--pipe", name, "--poll-ms", "500"]
        spec = "serial:*calder-wpf*"
    else:
        server = serve_tcp(SyringePump())
        args = ["--protocol", "pump", "--tcp", server.address, "--poll-ms", "500"]
        spec = f"tcp:{server.address}"
    try:
        session = record(
            RecordOptions(
                app=str(vendorapp(arch)),
                args=args,
                filter=CaptureFilter.parse(spec),
                ui=True,
                drive_script=str(HERE / f"{protocol}_script.json"),
            )
        )
    finally:
        server.stop()
    return save_session(session, tmp_path / f"{protocol}-{arch}.libsession")


@pytest.mark.parametrize("arch", ARCHES)
def test_vendor_app_thermostat_over_pipe(arch: str, tmp_path: Path):
    _require(arch, app=True)
    session = load_session(_record_app(arch, "thermostat", tmp_path))
    assert session.meta.app.bitness == (64 if arch == "x64" else 32)
    ui = {(u.kind.value, u.element.automation_id) for u in session.ui_events}
    assert {("invoke", "ApplyButton"), ("value", "SetpointBox"), ("selection", "ModeSelector")} <= ui
    model = infer([session], InferOptions(name="vendor_thermostat"))
    assert model.family.value == "scpi"
    setpoint = next(m for m in model.messages if m.key == "SOUR:TEMP")
    (field,) = [f for f in setpoint.fields if f.role is FieldRole.VALUE]
    assert (field.name, field.units, field.ui_element) == ("setpoint", "°C", "SetpointBox")
    (mode,) = [f for f in next(m for m in model.messages if m.key == "MODE").fields if f.role is FieldRole.ENUM]
    assert {e.wire: e.label for e in mode.enum} == {"CONST": "Constant", "RAMP": "Ramp", "HOLD": "Hold"}
    outp = next(m for m in model.messages if m.key == "OUTP")
    assert {b.element_name: b.constants.get("f0") for b in outp.ui_bindings if b.kind == "invoke"} == {
        "Start": "ON",
        "Stop": "OFF",
    }


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", *args], cwd=cwd, capture_output=True, text=True, timeout=600)


@pytest.mark.parametrize("arch", ARCHES)
def test_vendor_app_pump_full_flow(arch: str, tmp_path: Path):
    """record -> infer -> generate -> generated tests (driver, MCP, SiLA 2) -> verify."""
    _require(arch, app=True)
    session = load_session(_record_app(arch, "pump", tmp_path))
    model = infer([session], InferOptions(name="vendor_pump"))
    assert model.checksum is not None
    assert model.checksum.algorithm == "CRC-16/IBM-3740"
    volume = next(m for m in model.messages if m.key == "11" and m.direction.value == "tx")
    (field,) = [f for f in volume.fields if f.role is FieldRole.VALUE]
    assert (field.label, field.units) == ("Volume (mL)", "mL")
    model_path = save_model(model, tmp_path / "model.json")
    runner = CliRunner()
    out = tmp_path / "driver"
    result = runner.invoke(app, ["generate", str(model_path), "--out", str(out)])
    assert result.exit_code == 0, result.output
    tests = _run(["pytest", "-q", "-p", "no:cacheprovider"], out)
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert "test_mcp_lists_tools_and_guards_actuation" in (out / "tests" / "test_servers.py").read_text()
    with serve_tcp(SyringePump()) as server:
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
                str(tmp_path / "v.jsonl"),
            ],
        )
    assert result.exit_code == 0, result.output


def test_record_cli(tmp_path: Path):
    _require("x64", app=True)
    server = serve_tcp(SyringePump())
    try:
        result = CliRunner().invoke(
            app,
            [
                "record",
                "--app",
                str(vendorapp("x64")),
                "--args",
                f"--protocol pump --tcp {server.address}",
                "--filter",
                f"tcp:{server.address}",
                "--ui",
                "--drive",
                str(HERE / "pump_script.json"),
                "--out",
                str(tmp_path / "s.libsession"),
            ],
        )
    finally:
        server.stop()
    assert result.exit_code == 0, result.output
    session = load_session(tmp_path / "s.libsession")
    assert session.data_events()
    assert session.ui_events
