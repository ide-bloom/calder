"""The safety envelope of `calder verify` (docs/safety.md)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from calder.model.io import to_runtime
from calder.runtime.protocol import Codec, ProtocolSpec
from calder.runtime.transport import open_transport
from calder.verify.engine import FieldLimit, Limits, Phase, SafetyError, SafetyGuard, Verifier
from tests.conftest import model_for
from tests.simulators import pump
from tests.simulators.base import serve_tcp
from tests.simulators.thermostat import Thermostat


@pytest.fixture
def pump_spec() -> ProtocolSpec:
    return ProtocolSpec.from_dict(to_runtime(model_for("pump")))


def verifier(spec: ProtocolSpec, address: str, **kwargs: object) -> Verifier:
    transport = open_transport(spec.transport, f"tcp://{address}")
    kwargs.setdefault("rate_hz", 20.0)
    return Verifier(spec, transport, **kwargs)  # type: ignore[arg-type]


def test_refuses_unobserved_commands(pump_spec):
    guard = SafetyGuard(pump_spec, Phase.EXPLORE, Limits())
    with pytest.raises(SafetyError, match="unobserved"):
        guard.check(pump.frame(0x7F))  # an opcode never seen
    with pytest.raises(SafetyError, match="unobserved"):
        guard.check(b"\x02garbage")


def test_read_phase_only_sends_recorded_queries(pump_spec):
    guard = SafetyGuard(pump_spec, Phase.READ)
    recorded_query = next(e.request for e in pump_spec.exchanges if e.message == "tx_01")
    guard.check(recorded_query)
    start = next(e.request for e in pump_spec.exchanges if e.message == "tx_20")
    with pytest.raises(SafetyError, match="read-only"):
        guard.check(start)


def test_replay_needs_allow_actuation_and_confirmation(pump_spec):
    with serve_tcp(pump.SyringePump()) as server:
        with pytest.raises(SafetyError, match="allow-actuation"):
            verifier(pump_spec, server.address, phase=Phase.REPLAY).run()
        asked: list[str] = []

        def deny(cls: str, names: list[str]) -> bool:
            asked.append(cls)
            return False

        v = verifier(pump_spec, server.address, phase=Phase.REPLAY, allow_actuation=True, confirm=deny)
        with pytest.raises(SafetyError, match="not confirmed"):
            v.run()
        assert asked == ["configuring"]
        assert server.simulator.received == []  # nothing was transmitted


def test_replay_with_confirmation_runs(pump_spec, tmp_path: Path):
    with serve_tcp(pump.SyringePump()) as server:
        v = verifier(
            pump_spec,
            server.address,
            phase=Phase.REPLAY,
            allow_actuation=True,
            confirm=lambda cls, names: True,
            log_path=tmp_path / "log.jsonl",
        )
        result = v.run()
    assert not result.aborted, result.reason
    assert result.sent == result.passed > 0
    lines = [json.loads(x) for x in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    sent = b"".join(bytes.fromhex(x["hex"]) for x in lines if x["dir"] == "tx")
    assert sent == b"".join(server.simulator.received)  # every transmitted byte is logged


def test_explore_needs_limits_and_stays_in_range(pump_spec):
    with serve_tcp(pump.SyringePump()) as server:
        with pytest.raises(SafetyError, match="limits"):
            verifier(
                pump_spec, server.address, phase=Phase.EXPLORE, allow_actuation=True, confirm=lambda c, n: True
            ).run()
        rate = next(f for f in pump_spec.by_name("set_rate").fields if f.name == "rate")
        assert rate.minimum is not None and rate.maximum is not None
        limits = Limits({"set_rate": {"rate": FieldLimit(rate.minimum, rate.maximum * 10)}})
        result = verifier(
            pump_spec,
            server.address,
            phase=Phase.EXPLORE,
            allow_actuation=True,
            limits=limits,
            confirm=lambda c, n: True,
        ).run()
        assert not result.aborted, result.reason
    sent_rates = []
    codec = Codec(pump_spec)
    for frame in server.simulator.received:
        decoded = codec.decode(frame, direction="tx")
        if decoded.message.name == "set_rate":
            sent_rates.append(decoded.values["rate"])
    assert sent_rates
    assert all(rate.minimum - 1e-6 <= r <= rate.maximum + 1e-6 for r in sent_rates)


def test_out_of_range_parameters_are_refused(pump_spec):
    rate = next(f for f in pump_spec.by_name("set_rate").fields if f.name == "rate")
    assert rate.maximum is not None
    codec = Codec(pump_spec)
    too_high = codec.encode(pump_spec.by_name("set_rate"), {"rate": rate.maximum * 2})
    guard = SafetyGuard(pump_spec, Phase.EXPLORE, Limits({"set_rate": {"rate": FieldLimit(0, rate.maximum * 4)}}))
    with pytest.raises(SafetyError, match="observed range"):
        guard.check(too_high)
    narrow = SafetyGuard(pump_spec, Phase.EXPLORE, Limits({"set_rate": {"rate": FieldLimit(0, 0.5)}}))
    with pytest.raises(SafetyError, match="outside the limits"):
        narrow.check(codec.encode(pump_spec.by_name("set_rate"), {"rate": 1.0}))
    unlisted = SafetyGuard(pump_spec, Phase.EXPLORE, Limits())
    with pytest.raises(SafetyError, match="not listed"):
        unlisted.check(too_high)


def test_aborts_on_injected_error_status(pump_spec):
    with serve_tcp(pump.SyringePump(fault="stall")) as server:
        result = verifier(pump_spec, server.address, phase=Phase.READ).run()
    assert result.aborted
    assert "status" in result.reason


def test_aborts_on_unexpected_response():
    spec = ProtocolSpec.from_dict(to_runtime(model_for("thermostat")))
    with serve_tcp(Thermostat(fault="garbage")) as server:
        result = verifier(spec, server.address, phase=Phase.READ).run()
    assert result.aborted


def test_aborts_on_timeout(pump_spec):
    class Silent(pump.SyringePump):
        def respond(self, data: bytes) -> bytes:
            return b""

    with serve_tcp(Silent()) as server:
        result = verifier(pump_spec, server.address, phase=Phase.READ, timeout_scale=0.2).run()
    assert result.aborted
    assert "timeout" in result.reason


def test_rate_limit_and_warning(pump_spec):
    warnings: list[str] = []
    with serve_tcp(pump.SyringePump()) as server:
        v = verifier(pump_spec, server.address, phase=Phase.READ, rate_hz=5.0, warn=warnings.append)
        t0 = time.monotonic()
        result = v.run()
        elapsed = time.monotonic() - t0
    assert warnings and "emergency stop" in warnings[0]
    assert elapsed >= (result.sent - 1) / 5.0 - 0.05


def test_limits_file(tmp_path: Path):
    path = tmp_path / "limits.toml"
    path.write_text(
        "max_commands = 7\nrate_hz = 3\n[commands.set_rate]\nrate = { min = 0.5, max = 2.0 }\n", encoding="utf-8"
    )
    limits = Limits.load(path)
    assert limits.max_commands == 7
    assert limits.commands["set_rate"]["rate"] == FieldLimit(0.5, 2.0)
    bad = tmp_path / "bad.json"
    bad.write_text('{"commands": {"x": {"y": {"min": 3, "max": 1}}}}', encoding="utf-8")
    with pytest.raises(SafetyError):
        Limits.load(bad)


def test_fields_without_limits_keep_recorded_values():
    spec = ProtocolSpec.from_dict(to_runtime(model_for("thermostat")))
    codec = Codec(spec)
    guard = SafetyGuard(spec, Phase.EXPLORE, Limits({"sour_temp": {}}))
    recorded = next(e.request for e in spec.exchanges if e.message == "tx_sour_temp")
    value = codec.decode(recorded, direction="tx").values["setpoint"]
    guard.check(codec.encode(spec.by_name("sour_temp"), {"setpoint": value}))
    with pytest.raises(SafetyError, match="no limit"):
        guard.check(codec.encode(spec.by_name("sour_temp"), {"setpoint": 99.99}))
