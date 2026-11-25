from __future__ import annotations

import struct

from calder.infer.correlate import UiContext, correlate_enum, correlate_values, invoke_bindings, parse_label, snake
from calder.infer.fields import ascii_number_encoding, binary_candidates, segment_region, type_tokens
from calder.infer.streams import Frame
from calder.runtime.encodings import Encoding, EncodingKind, decode_binary, encode_ascii, encode_binary
from calder.session import Direction, SessionBuilder, Source, Transport, UiElement, UiKind


def test_float32_le_wins_for_typed_decimals():
    values = [2.5, 1.237, 10.0, 0.75, 33.1]
    spans = [struct.pack("<f", v) for v in values]
    best = binary_candidates(spans)[0]
    assert best.encoding.kind is EncodingKind.FLOAT
    assert best.encoding.endian == "le"
    assert best.values == values


def test_u16_little_endian_by_carry_pattern():
    values = [300, 1200, 50, 999, 1500, 77]
    spans = [v.to_bytes(2, "little") for v in values]
    best = binary_candidates(spans)[0]
    assert (best.encoding.kind, best.encoding.width, best.encoding.endian) == (EncodingKind.UINT, 2, "le")


def test_signed_detection_and_bcd():
    spans = [(v & 0xFFFF).to_bytes(2, "big") for v in (-50, 375, -5, 950)]
    best = binary_candidates(spans)[0]
    assert best.encoding.kind is EncodingKind.INT
    bcd = Encoding(EncodingKind.BCD, width=2, endian="be")
    assert decode_binary(bcd, bytes.fromhex("1234")) == 1234
    assert encode_binary(bcd, 42) == bytes.fromhex("0042")


def test_segmentation_keeps_padding_out_of_numbers():
    frames = [
        bytes([0x01, i, 0x01]) + (v).to_bytes(2, "little") + bytes(11)
        for i, v in enumerate([300, 1200, 50, 999, 1500, 77])
    ]
    segs = segment_region(frames, 3, 16)
    assert (segs[0].offset, segs[0].length, segs[0].candidate.encoding.width) == (3, 2, 2)  # type: ignore[union-attr]
    assert segs[-1].constant == bytes(11)


def test_ascii_number_encodings():
    enc = ascii_number_encoding(["+0012.345", "-0001.200"], fixed_width=True)
    assert enc is not None
    assert (enc.kind, enc.width, enc.decimals, enc.plus_sign, enc.zero_pad) == (
        EncodingKind.ASCII_FLOAT,
        9,
        3,
        True,
        True,
    )
    assert encode_ascii(enc, 12.345) == "+0012.345"
    assert encode_ascii(enc, -0.12) == "-0000.120"
    assert ascii_number_encoding(["1", "22", "333"]).kind is EncodingKind.ASCII_INT  # type: ignore[union-attr]
    assert type_tokens(["CONST", "RAMP", "HOLD", "RAMP"], tx=True).role == "enum"
    assert type_tokens(["ON", "ON", "ON"], tx=True).role == "constant"


def test_label_parsing():
    assert parse_label("Setpoint (°C)") == ("Setpoint", "°C")
    assert parse_label("Rate [mL/min]:") == ("Rate", "mL/min")
    assert parse_label("Mode") == ("Mode", None)
    assert snake("Actual speed") == "actual_speed"
    assert snake("IDN") == "idn"


def _ui_session() -> tuple[UiContext, list[Frame], list[float]]:
    b = SessionBuilder(Source.SYNTHETIC, session_id="s")
    box = UiElement(name="Setpoint", automation_id="SP", control_type="Edit", label="Setpoint (°C)")
    other = UiElement(name="Rate", automation_id="R", control_type="Edit", label="Rate")
    frames: list[Frame] = []
    values = [37.5, 20.0, -5.25, 80.0]
    for i, v in enumerate(values):
        t = (i + 1) * 3_000_000_000
        b.ui(t - 400_000_000, UiKind.VALUE, box, f"{v:g}")
        b.ui(t - 300_000_000, UiKind.VALUE, other, "1.5")
        frames.append(Frame(0, Direction.TX, t, b"x", (i,), seq=i))
    return UiContext([b.build()]), frames, values


def test_value_correlation_picks_matching_element():
    ui, frames, values = _ui_session()
    hit = correlate_values(ui, frames, values)
    assert hit is not None
    assert (hit.element, hit.label, hit.support) == ("SP", "Setpoint (°C)", 4)
    assert correlate_values(ui, frames, [v + 1 for v in values]) is None


def test_enum_correlation():
    b = SessionBuilder(Source.SYNTHETIC, session_id="s")
    combo = UiElement(name="Mode", automation_id="M", control_type="ComboBox", label="Mode")
    frames, wires = [], []
    for i, (choice, wire) in enumerate([("Ramp", "RAMP"), ("Hold", "HOLD"), ("Ramp", "RAMP"), ("Constant", "CONST")]):
        t = (i + 1) * 2_000_000_000
        b.ui(t - 50_000_000, UiKind.SELECTION, combo, choice)
        frames.append(Frame(0, Direction.TX, t, wire.encode(), (i,), seq=i))
        wires.append(wire)
    mapping = correlate_enum(UiContext([b.build()]), frames, wires)
    assert mapping is not None
    assert mapping.mapping == {"RAMP": "Ramp", "HOLD": "Hold", "CONST": "Constant"}


def test_invoke_bindings_use_lift_over_baseline():
    b = SessionBuilder(Source.SYNTHETIC, session_id="s")
    start = UiElement(name="Start", automation_id="StartButton", control_type="Button")
    frames: list[Frame] = []
    seq = 0
    for i in range(40):  # a 1 Hz poll the button must not be bound to
        frames.append(Frame(0, Direction.TX, i * 1_000_000_000, b"POLL", (seq,), seq=seq, type_id="poll"))
        seq += 1
    for t in (5_300_000_000, 17_300_000_000, 29_300_000_000):
        b.ui(t, UiKind.INVOKE, start)
        frames.append(Frame(0, Direction.TX, t + 30_000_000, b"GO", (seq,), seq=seq, type_id="go"))
        seq += 1
    b.wire(0, Transport.SERIAL, "serial:COM1", Direction.TX, b"x")
    session = b.build()
    bindings = invoke_bindings(UiContext([session]), frames, [session])
    assert [(x.name, x.type_id) for x in bindings] == [("Start", "go")]
