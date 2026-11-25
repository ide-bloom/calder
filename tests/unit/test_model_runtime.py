from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from tests.conftest import ROOT, model_for, sessions_for

from calder.model.io import (
    ModelValidationError,
    load_model,
    model_json_schema,
    save_model,
    to_runtime,
    validate_model,
)
from calder.model.schema import Status
from calder.runtime.client import PreconditionError, ProtocolClient
from calder.runtime.encodings import Encoding, EncodingError, EncodingKind, encode_binary, shortest_float32
from calder.runtime.protocol import Codec, CodecError, ProtocolSpec
from calder.runtime.simulator import ReplaySimulator, SimulatorServer
from calder.runtime.transport import LoopbackTransport, SocketTransport, open_transport
from calder.session import SessionFormatError, dumps_session, load_session, loads_session, save_session


def test_session_round_trip(tmp_path):
    s = sessions_for("pump")[0]
    path = save_session(s, tmp_path / "a.libsession")
    again = load_session(path)
    assert again.events == s.events
    assert again.ui_events == s.ui_events
    assert again.meta.session_id == s.meta.session_id


def test_session_format_errors():
    with pytest.raises(SessionFormatError):
        loads_session(b"not a zip")
    src = zipfile.ZipFile(io.BytesIO(dumps_session(sessions_for("pump")[0])))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for name in src.namelist():
            data = src.read(name)
            if name == "meta.json":
                data = data.replace(b'"format_version": "1.0"', b'"format_version": "9.0"')
            out.writestr(name, data)
    with pytest.raises(SessionFormatError):
        loads_session(buf.getvalue())


def test_model_save_load_and_validation(tmp_path):
    m = model_for("thermostat")
    path = save_model(m, tmp_path / "m.json")
    assert load_model(path) == m
    broken = m.model_copy(deep=True)
    broken.messages[0].response = "nope"
    with pytest.raises(ModelValidationError):
        validate_model(broken)
    dup = m.model_copy(deep=True)
    dup.messages[1].name = dup.messages[0].name
    with pytest.raises(ModelValidationError):
        validate_model(dup)


def test_json_schema_matches_docs():
    schema = model_json_schema()
    assert schema["title"] == "Calder protocol model"
    documented = ROOT / "docs" / "protocol-model.schema.json"
    assert json.loads(documented.read_text(encoding="utf-8")) == schema


def test_rejected_messages_leave_the_runtime():
    m = model_for("thermostat").model_copy(deep=True)
    target = next(x for x in m.messages if x.key == "SYST:ERR?")
    target.status = Status.REJECTED
    rt = to_runtime(m)
    assert target.id not in {x["id"] for x in rt["messages"]}
    assert all(e["message"] != target.id for e in rt["exchanges"])


def test_encodings():
    assert shortest_float32(2.299999952316284) == 2.3
    with pytest.raises(EncodingError):
        encode_binary(Encoding(EncodingKind.UINT, width=1), 300)
    enc = Encoding(EncodingKind.INT, width=2, endian="be", scale_exp=-1)
    assert encode_binary(enc, -5.0) == (-50 & 0xFFFF).to_bytes(2, "big")


@pytest.fixture
def pump_spec() -> ProtocolSpec:
    return ProtocolSpec.from_dict(to_runtime(model_for("pump")))


def test_codec_encode_decode(pump_spec):
    codec = Codec(pump_spec)
    msg = pump_spec.by_name("set_rate")
    frame = codec.encode(msg, {"rate": 2.5})
    assert codec.decode(frame, direction="tx").values == {"rate": 2.5}
    with pytest.raises(CodecError):
        codec.decode(frame[:-1] + bytes([frame[-1] ^ 1]), direction="tx")  # checksum mismatch


def test_client_enforces_preconditions(pump_spec):
    client = ProtocolClient(pump_spec, LoopbackTransport(ReplaySimulator(pump_spec)))
    with pytest.raises(PreconditionError):
        client.send("tx_20")
    client.send("tx_10", {"rate": 1.0})
    client.send("tx_11", {"volume": 2.0})
    assert client.send("tx_20") is not None


def test_simulator_reflects_set_values(pump_spec):
    client = ProtocolClient(pump_spec, LoopbackTransport(ReplaySimulator(pump_spec)), check_preconditions=False)
    client.send("tx_10", {"rate": 7.25})
    reply = client.send("tx_30")
    assert reply is not None
    assert reply.values["rate"] == 7.25


def test_simulator_over_tcp(pump_spec):
    with SimulatorServer(ReplaySimulator(pump_spec)) as server:
        transport = open_transport(pump_spec.transport, f"tcp://{server.address}")
        assert isinstance(transport, SocketTransport)
        client = ProtocolClient(pump_spec, transport, check_preconditions=False)
        assert client.send("tx_01") is not None
        client.close()


def test_runtime_is_standard_library_only():
    for path in (ROOT / "src" / "calder" / "runtime").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from calder" not in text and "import calder" not in text, path
        for third_party in ("pydantic", "numpy", "typer"):
            assert f"import {third_party}" not in text, path


def test_paths_exist():
    assert Path(ROOT / "src" / "calder" / "runtime" / "protocol.py").is_file()
