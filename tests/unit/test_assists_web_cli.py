"""Manual assist, naming assist, Modbus variants, review UI and CLI."""

from __future__ import annotations

import json
import zlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from tests.conftest import model_for, sessions_for
from tests.simulators.sessions import modbus_session
from typer.testing import CliRunner

from calder.cli import app
from calder.infer import manual
from calder.infer.naming import suggest
from calder.infer.pipeline import InferOptions, infer
from calder.model.io import load_model, save_model
from calder.session.io import save_session
from calder.web.app import create_app


def _pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF with text lines (Helvetica)."""
    content = (
        "BT /F1 11 Tf 50 750 Td 14 TL "
        + " ".join("(" + ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") '" for ln in lines)
        + " ET"
    )
    stream = zlib.compress(content.encode("latin-1"))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_manual_assist(tmp_path: Path):
    pytest.importorskip("pdfplumber", reason="install the manual extra: uv sync --all-extras")
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(
        _pdf(
            [
                "Command reference",
                "SOURce:TEMPerature <value> Sets the temperature setpoint in degrees C",
                "MEASure:TEMPerature? Returns the measured temperature",
                "*IDN? Identification query",
            ]
        )
    )
    entries, pages, digest = manual.extract(pdf)
    assert pages == 1
    assert len(digest) == 64
    assert manual.short_form("SOURce:TEMPerature") == "SOUR:TEMP"
    hit = manual.match(entries, "SOUR:TEMP")
    assert hit is not None
    assert "setpoint" in hit.description
    model = infer(list(sessions_for("thermostat")), InferOptions(manual=str(pdf)))
    assert model.manual is not None
    sour = next(m for m in model.messages if m.key == "SOUR:TEMP")
    assert sour.manual is not None
    assert sour.doc.startswith("Sets the temperature")


def test_naming_assist_marks_suggestions_only():
    model = model_for("pump").model_copy(deep=True)
    target = model.messages[0]
    reply = {
        "messages": {target.id: {"name": "read_pump_status", "doc": "Polls the pump."}},
        "fields": {f"{target.id}.f0": {"name": "Bad Name!", "doc": "x"}},
    }
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    old_name = target.name
    applied = suggest(model, endpoint="http://llm.local/v1", model_name="m", api_key="k", client=client)
    assert applied == 1
    assert target.name == old_name
    assert target.suggested_name == "read_pump_status"
    assert model.messages[0].fields[0].suggested_name is None  # invalid identifiers are dropped
    assert seen["auth"] == "Bearer k"


@pytest.mark.parametrize("mode", ["ascii", "tcp"])
def test_modbus_variants(mode):
    model = infer([modbus_session(300 + i, mode) for i in range(8)])
    assert model.family.value == f"modbus_{mode}"
    assert not [w for w in model.warnings if "does not decode" in w]
    keys = {m.key for m in model.messages if m.direction.value == "tx"}
    assert {"03:0000x3", "04:0000x1", "06:0000", "06:0001"} <= keys


# ----- review UI -----------------------------------------------------------------------------


@pytest.fixture
def review(tmp_path: Path) -> tuple[TestClient, Path]:
    session_path = save_session(sessions_for("thermostat")[0], tmp_path / "s.libsession")
    model = model_for("thermostat").model_copy(deep=True)
    model.sessions[0].path = session_path.name
    path = save_model(model, tmp_path / "model.json")
    return TestClient(create_app(path, "secret-token", actor="tester")), path


def test_review_requires_token(review):
    client, _ = review
    assert client.get("/api/model").status_code == 403
    assert client.get("/").status_code == 403
    page = client.get("/?t=secret-token")
    assert page.status_code == 200
    assert "Calder review" in page.text
    assert "cdn" not in page.text.lower()
    assert client.get("/api/model", headers={"x-calder-token": "secret-token"}).json()["name"] == "thermostat"
    bad_host = client.get("/api/model", headers={"x-calder-token": "secret-token", "host": "evil.example"})
    assert bad_host.status_code == 403


def test_review_edits_are_saved_with_audit(review):
    client, path = review
    headers = {"x-calder-token": "secret-token"}
    msg = next(m for m in load_model(path).messages if m.key == "SOUR:TEMP")
    r = client.post(
        "/api/edit", headers=headers, json={"target": f"message:{msg.id}", "action": "rename", "value": "set_setpoint"}
    )
    assert r.status_code == 200
    r = client.post(
        "/api/edit", headers=headers, json={"target": f"message:{msg.id}", "action": "classify", "value": "actuating"}
    )
    assert r.status_code == 200
    bad = client.post(
        "/api/edit",
        headers=headers,
        json={"target": f"message:{msg.id}", "action": "rename", "value": "Not An Identifier"},
    )
    assert bad.status_code == 400
    saved = load_model(path)
    edited = saved.message(msg.id)
    assert edited.name == "set_setpoint"
    assert (edited.classification.value, edited.classification_confirmed) == ("actuating", True)
    assert [(a.actor, a.action) for a in saved.audit] == [("tester", "rename"), ("tester", "classify")]
    timeline = client.get("/api/timeline", headers=headers).json()
    assert {r["source"] for r in timeline} == {"wire", "ui"}


# ----- CLI -----------------------------------------------------------------------------------


def test_cli_infer_generate(tmp_path: Path):
    runner = CliRunner()
    paths = [str(save_session(s, tmp_path / f"{i}.libsession")) for i, s in enumerate(sessions_for("pump")[:6])]
    result = runner.invoke(app, ["infer", *paths, "--out", str(tmp_path / "m.json"), "--name", "pump"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["generate", str(tmp_path / "m.json"), "--out", str(tmp_path / "gen")])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "gen" / "liberated_pump" / "protocol.json").is_file()
    again = runner.invoke(app, ["generate", str(tmp_path / "m.json"), "--out", str(tmp_path / "gen")])
    assert again.exit_code == 1  # refuses to overwrite without --force


def test_cli_version_and_doctor():
    runner = CliRunner()
    assert runner.invoke(app, ["--version"]).output.startswith("calder ")
    result = runner.invoke(app, ["doctor"])
    assert "Python" in result.output
