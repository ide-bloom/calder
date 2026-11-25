"""Inference must recover exactly the protocol each simulator implements."""

from __future__ import annotations

from calder.model.schema import EncodingKind, FieldRole, MessageType, ProtocolModel
from calder.runtime.protocol import normalize_header
from tests.conftest import model_for
from tests.simulators import balance, hid_stirrer, modbus_tc, pump, thermostat


def tx(model: ProtocolModel) -> list[MessageType]:
    return [m for m in model.messages if m.direction.value == "tx"]


def by_key(model: ProtocolModel, key: str, direction: str = "tx") -> MessageType:
    for m in model.messages:
        if m.direction.value == direction and normalize_header(m.key) == normalize_header(key):
            return m
    raise AssertionError(f"no {direction} message {key}; have {[m.key for m in model.messages]}")


def field(m: MessageType, role: FieldRole) -> list:
    return [f for f in m.fields if f.role is role]


def preconds(model: ProtocolModel, m: MessageType) -> set[str]:
    return {model.message(p.message).key for p in m.preconditions}


def test_thermostat():
    truth = thermostat.GROUND_TRUTH
    m = model_for("thermostat")
    assert m.family.value == "scpi"
    assert m.framing.kind.value == "delimited"
    assert bytes.fromhex(m.framing.terminator_tx) == truth["terminator"]
    assert m.checksum is None
    assert {normalize_header(x.key) for x in tx(m)} == truth["commands"]
    for key, decimals in truth["float_args"].items():
        (f,) = [f for f in by_key(m, key).fields if f.role is FieldRole.VALUE]
        assert f.encoding is not None
        assert (f.encoding.kind, f.encoding.decimals) == (EncodingKind.ASCII_FLOAT, decimals)
    for key, values in truth["enums"].items():
        (f,) = field(by_key(m, key), FieldRole.ENUM)
        assert {e.wire for e in f.enum} == values
    (mode,) = field(by_key(m, "MODE"), FieldRole.ENUM)
    assert {e.wire: e.label for e in mode.enum} == truth["enum_labels"]["MODE"]
    rem = by_key(m, truth["remote_on"])
    loc = by_key(m, truth["remote_off"])
    for x in tx(m):
        expected = {truth["remote_on"]} if normalize_header(x.key) in truth["remote_required"] else set()
        assert preconds(m, x) == expected, x.key
        for p in x.preconditions:
            assert p.message == rem.id
            assert p.disabled_by == [loc.id]
    (setpoint,) = field(by_key(m, "SOUR:TEMP"), FieldRole.VALUE)
    assert (setpoint.name, setpoint.units) == ("setpoint", "°C")
    outp = by_key(m, "OUTP")
    assert outp.classification.value == "actuating"
    assert {b.element_name: b.constants for b in outp.ui_bindings if b.kind == "invoke"} == {
        "Start": {"f0": "ON"},
        "Stop": {"f0": "OFF"},
    }


def test_pump():
    truth = pump.GROUND_TRUTH
    m = model_for("pump")
    assert m.framing.kind.value == "length_prefixed"
    assert bytes.fromhex(m.framing.start) == truth["start"]
    lf = m.framing.length_field
    assert lf is not None
    assert (lf.offset, lf.width, lf.adjust) == tuple(truth["length"].values())
    assert m.checksum is not None
    ck = truth["checksum"]
    assert (m.checksum.algorithm, m.checksum.start, m.checksum.end, m.checksum.position, m.checksum.endian) == (
        ck["algorithm"],
        ck["start"],
        ck["end"],
        ck["position"],
        ck["endian"],
    )
    assert {x.key for x in tx(m)} == truth["tx"]
    assert {x.key for x in m.messages if x.direction.value == "rx"} == truth["rx"]
    for x in m.messages:
        (addr,) = field(x, FieldRole.ADDRESS)
        assert (addr.offset, addr.constant) == truth["address"]
        (op,) = field(x, FieldRole.OPCODE)
        assert op.offset == truth["opcode_offset"]
    for (direction, key), offset in truth["floats"].items():
        f = next(f for f in by_key(m, key, direction).fields if f.offset == offset)
        assert f.encoding is not None
        assert (f.encoding.kind, f.encoding.width, f.encoding.endian) == (EncodingKind.FLOAT, 4, "le")
    for (direction, key), (offset, labels) in truth["enums"].items():
        f = next(f for f in by_key(m, key, direction).fields if f.offset == offset)
        assert f.role is FieldRole.ENUM
        assert {e.wire: e.label for e in f.enum} == labels
    for x in m.messages:
        if x.direction.value == "rx":
            (status,) = field(x, FieldRole.STATUS)
            assert (status.offset, status.status_ok) == (truth["status_offset"], ["0"])
    for x in tx(m):
        assert preconds(m, x) == truth["preconditions"].get(x.key, set()), x.key


def test_modbus():
    truth = modbus_tc.GROUND_TRUTH
    m = model_for("modbus")
    assert m.family.value == truth["family"]
    assert m.framing.kind.value == "modbus_rtu"
    assert m.checksum is not None
    ck = truth["checksum"]
    assert (m.checksum.algorithm, m.checksum.start, m.checksum.position, m.checksum.endian) == (
        ck["algorithm"],
        ck["start"],
        ck["position"],
        ck["endian"],
    )
    assert {x.key for x in tx(m)} == truth["tx"]
    for name in ("setpoint", "pv"):
        key, offset, kind, endian, scale = truth[name]
        direction = "rx" if key.startswith("response:") else "tx"
        f = next(f for f in by_key(m, key, direction).fields if f.offset == offset)
        assert f.encoding is not None
        assert (f.encoding.kind.value, f.encoding.width, f.encoding.endian, f.encoding.scale_exp) == (
            kind,
            2,
            endian,
            scale,
        )
    key, offset, labels = truth["mode"]
    f = next(f for f in by_key(m, key).fields if f.offset == offset)
    assert f.role is FieldRole.ENUM
    assert {e.wire: e.label for e in f.enum} == labels
    assert all(not x.preconditions for x in tx(m))


def test_balance():
    truth = balance.GROUND_TRUTH
    m = model_for("balance")
    assert bytes.fromhex(m.framing.terminator_rx) == truth["terminator"]
    assert {x.key for x in tx(m)} == truth["tx"]
    (stream,) = [x for x in m.messages if x.kind.value == "unsolicited"]
    assert stream.layout == "columns"
    assert sum(f.length or 0 for f in stream.fields) == truth["stream_width"]
    offset, length, states = truth["stream_fields"]["state"]
    state = next(f for f in stream.fields if f.offset == offset)
    assert (state.role, state.length, {e.wire for e in state.enum}) == (FieldRole.ENUM, length, states)
    offset, width, decimals = truth["stream_fields"]["weight"]
    weight = next(f for f in stream.fields if f.offset == offset)
    assert weight.encoding is not None
    assert (weight.encoding.kind, weight.encoding.width, weight.encoding.decimals, weight.encoding.plus_sign) == (
        EncodingKind.ASCII_FLOAT,
        width,
        decimals,
        True,
    )
    assert all(not x.preconditions for x in tx(m))


def test_hid():
    truth = hid_stirrer.GROUND_TRUTH
    m = model_for("hid")
    assert m.framing.kind.value == truth["framing"]
    assert {x.key for x in tx(m)} == truth["tx"]
    for x in m.messages:
        if x.direction.value == "tx":
            (seq,) = field(x, FieldRole.COUNTER)
            assert seq.offset == truth["counter_offset"]
        else:
            (echo,) = field(x, FieldRole.ECHO)
            assert echo.offset == truth["echo_offset"]
            (status,) = field(x, FieldRole.STATUS)
            assert status.offset == truth["status_offset"]
    key, offset, width, endian = truth["speed"]
    f = next(f for f in by_key(m, key).fields if f.offset == offset)
    assert f.encoding is not None
    assert (f.encoding.kind, f.encoding.width, f.encoding.endian, f.units) == (EncodingKind.UINT, width, endian, "rpm")
    key, offset, labels = truth["mode"]
    f = next(f for f in by_key(m, key).fields if f.offset == offset)
    assert {e.wire: e.label for e in f.enum} == labels
    for x in tx(m):
        assert preconds(m, x) == truth["preconditions"].get(x.key, set()), x.key


def test_every_frame_round_trips(kind):
    """The self-check found no frame the generated codec would disagree with."""
    m = model_for(kind)
    assert not [w for w in m.warnings if "does not decode" in w or "re-encoding" in w]
