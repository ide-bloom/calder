"""Programmatic sessions: a simulated vendor application driving each simulator.

Each generator plays a randomized but realistic operator session against a
simulator on a virtual clock and records wire and UI events exactly as the
capture layer would, so inference can be checked against ground truth.
"""

from __future__ import annotations

import random
import struct
from collections.abc import Callable
from typing import Any

from calder.session import Direction, EventKind, Session, SessionBuilder, Source, Transport, UiElement, UiKind
from tests.simulators import hid_stirrer, modbus_tc, pump
from tests.simulators.balance import Balance
from tests.simulators.base import Instrument
from tests.simulators.thermostat import MODES as THERMO_MODES
from tests.simulators.thermostat import Thermostat

MS = 1_000_000


class Recorder:
    """Plays the vendor application: UI actions, wire traffic and a virtual clock."""

    def __init__(
        self,
        instrument: Instrument,
        transport: Transport,
        stream: str,
        *,
        seed: int,
        open_meta: dict[str, Any] | None = None,
        config_meta: dict[str, Any] | None = None,
        description: str = "",
    ) -> None:
        self.sim = instrument
        self.transport = transport
        self.stream = stream
        self.rng = random.Random(seed)
        self.builder = SessionBuilder(
            Source.SYNTHETIC, session_id=f"{stream.split(':', maxsplit=1)[0]}-{seed:04d}", description=description
        )
        self.t = 1_000 * MS
        self.builder.wire(self.t, transport, stream, Direction.NONE, b"", EventKind.OPEN, meta=open_meta or {})
        if config_meta:
            self.t += 2 * MS
            self.builder.wire(self.t, transport, stream, Direction.NONE, b"", EventKind.CONFIG, meta=config_meta)
        self.stream_period: int | None = None
        self._next_stream = 0

    # ----- clock --------------------------------------------------------------------------

    def wait(self, ms: float, poll: Callable[[], None] | None = None, poll_every_ms: float = 1000) -> None:
        end = self.t + int(ms * MS)
        next_poll = self.t + int(poll_every_ms * MS)
        while self.t < end:
            step_to = min(end, next_poll if poll else end)
            if self.stream_period:
                while self._next_stream <= step_to:
                    self.t = max(self.t, self._next_stream)
                    data = self.sim.poll()
                    if data:
                        self.rx(data)
                    self._next_stream += self.stream_period
            self.sim.advance((step_to - self.t) / 1e9)
            self.t = step_to
            if poll and self.t >= next_poll and self.t < end:
                poll()
                next_poll = self.t + int(poll_every_ms * MS * self.rng.uniform(0.95, 1.05))

    # ----- wire ---------------------------------------------------------------------------

    def tx(self, data: bytes) -> None:
        self.builder.wire(self.t, self.transport, self.stream, Direction.TX, data)

    def rx(self, data: bytes) -> None:
        self.builder.wire(self.t, self.transport, self.stream, Direction.RX, data)

    def exchange(self, frame: bytes, *, split_reads: bool = False) -> bytes:
        self.tx(frame)
        self.t += int(self.rng.uniform(4, 25) * MS)
        out = self.sim.handle(frame)
        data = out if isinstance(out, bytes) else b"".join(out)
        if data:
            if split_reads and len(data) > 3 and self.rng.random() < 0.5:
                cut = self.rng.randint(1, len(data) - 1)
                self.rx(data[:cut])
                self.t += int(self.rng.uniform(1, 3) * MS)
                self.rx(data[cut:])
            else:
                self.rx(data)
        self.t += int(self.rng.uniform(2, 10) * MS)
        return data

    # ----- UI -----------------------------------------------------------------------------

    def ui(self, kind: UiKind, element: UiElement, value: str | None = None) -> None:
        self.builder.ui(self.t, kind, element, value)
        self.t += int(self.rng.uniform(20, 80) * MS)

    def build(self) -> Session:
        self.t += 5 * MS
        self.builder.wire(self.t, self.transport, self.stream, Direction.NONE, b"", EventKind.CLOSE)
        return self.builder.build()


def button(automation_id: str, name: str) -> UiElement:
    return UiElement(name=name, automation_id=automation_id, control_type="Button", window="Vendor")


def edit(automation_id: str, label: str) -> UiElement:
    return UiElement(name=label, automation_id=automation_id, control_type="Edit", label=label, window="Vendor")


def combo(automation_id: str, label: str) -> UiElement:
    return UiElement(name=label, automation_id=automation_id, control_type="ComboBox", label=label, window="Vendor")


def display(automation_id: str, label: str) -> UiElement:
    return UiElement(name=label, automation_id=automation_id, control_type="Text", label=label, window="Vendor")


def _fmt(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


SERIAL_9600 = {"baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1, "rtscts": False, "xonxoff": False}


# ----- thermostat (SCPI) -----------------------------------------------------------------


def thermostat_session(seed: int) -> Session:
    sim = Thermostat()
    r = Recorder(
        sim,
        Transport.SERIAL,
        "serial:COM5",
        seed=seed,
        open_meta={"path": "COM5"},
        config_meta=SERIAL_9600,
        description="thermostat",
    )
    rng = r.rng
    sp_box, rate_box = edit("SetpointBox", "Setpoint (°C)"), edit("RateBox", "Rate (°C/min)")
    mode_sel = combo("ModeSelector", "Mode")
    temp_display = display("TemperatureDisplay", "Temperature (°C)")
    remote = False

    def send(cmd: str) -> str:
        return r.exchange(cmd.encode() + b"\n").decode().strip()

    def poll() -> None:
        reply = send("MEAS:TEMP?")
        if reply:
            r.ui(UiKind.VALUE, temp_display, reply)

    def ensure_remote() -> None:
        nonlocal remote
        if not remote:
            send("SYST:REM")
            remote = True

    r.ui(UiKind.INVOKE, button("ConnectButton", "Connect"))
    send("*IDN?")
    if rng.random() < 0.5:
        r.wait(rng.uniform(1200, 2500), poll)
    actions = ["apply", "apply", "mode", "start", "stop", "read", "local", "errors", "mode", "start"]
    rng.shuffle(actions)
    started = False
    for action in actions[: rng.randint(6, len(actions))]:
        r.wait(rng.uniform(1100, 2600), poll)
        if action == "apply":
            sp = round(rng.uniform(-15, 120) * 4) / 4
            rate = round(rng.uniform(0.5, 5.0) * 10) / 10
            r.ui(UiKind.VALUE, sp_box, _fmt(sp))
            if rng.random() < 0.8:
                r.ui(UiKind.VALUE, rate_box, _fmt(rate))
            else:
                rate = sim.rate
            r.ui(UiKind.INVOKE, button("ApplyButton", "Apply"))
            ensure_remote()
            send(f"SOUR:TEMP {sp:.2f}")
            send(f"SOUR:TEMP:RATE {rate:.2f}")
        elif action == "mode":
            mode = rng.choice(list(THERMO_MODES))
            r.ui(UiKind.SELECTION, mode_sel, THERMO_MODES[mode])
            ensure_remote()
            send(f"MODE {mode}")
        elif action == "start":
            r.ui(UiKind.INVOKE, button("StartButton", "Start"))
            ensure_remote()
            send("OUTP ON")
            started = True
        elif action == "stop":
            r.ui(UiKind.INVOKE, button("StopButton", "Stop"))
            ensure_remote()
            send("OUTP OFF")
            started = False
        elif action == "read":
            r.ui(UiKind.INVOKE, button("ReadButton", "Read setpoint"))
            send("SOUR:TEMP?")
        elif action == "local":
            r.ui(UiKind.INVOKE, button("LocalButton", "Local"))
            send("SYST:LOC")
            remote = False
        elif action == "errors":
            r.ui(UiKind.INVOKE, button("ErrorsButton", "Errors"))
            send("SYST:ERR?")
    r.wait(rng.uniform(1100, 2000), poll)
    if started:
        r.ui(UiKind.INVOKE, button("StopButton", "Stop"))
        ensure_remote()
        send("OUTP OFF")
    r.ui(UiKind.INVOKE, button("DisconnectButton", "Disconnect"))
    send("SYST:LOC")
    return r.build()


# ----- syringe pump (binary, CRC-16) ------------------------------------------------------


def pump_session(seed: int) -> Session:
    sim = pump.SyringePump()
    r = Recorder(
        sim,
        Transport.SERIAL,
        "serial:COM7",
        seed=seed,
        open_meta={"path": "COM7"},
        config_meta={**SERIAL_9600, "baudrate": 19200},
        description="syringe pump",
    )
    rng = r.rng
    vol_box, rate_box = edit("VolumeBox", "Volume (mL)"), edit("RateBox", "Rate (mL/min)")
    dir_sel = combo("DirectionSelector", "Direction")
    dispensed = display("DispensedDisplay", "Dispensed (mL)")
    rate_set = vol_set = False

    def send(op: int, payload: bytes = b"") -> bytes:
        return r.exchange(pump.frame(op, payload), split_reads=True)

    def poll() -> None:
        reply = send(pump.GET_STATUS)
        if len(reply) >= 12:
            (value,) = struct.unpack("<f", reply[6:10])
            r.ui(UiKind.VALUE, dispensed, f"{value:.3f}")

    r.ui(UiKind.INVOKE, button("ConnectButton", "Connect"))
    send(pump.GET_STATUS)
    actions = ["rate", "volume", "direction", "read", "stop", "direction"]
    rng.shuffle(actions)
    actions = actions[: rng.randint(4, len(actions))]
    for needed in ("rate", "volume"):
        if needed not in actions:
            actions.insert(rng.randint(0, len(actions)), needed)
    ready = max(actions.index("rate"), actions.index("volume"))
    actions.insert(rng.randint(ready + 1, len(actions)), "start")
    if rng.random() < 0.6:
        actions.insert(rng.randint(actions.index("start") + 1, len(actions)), "stop")
    for action in actions:
        r.wait(rng.uniform(1100, 2600), poll)
        if action == "rate":
            value = round(rng.uniform(0.1, 20.0), rng.choice([1, 2, 3]))
            r.ui(UiKind.VALUE, rate_box, _fmt(value))
            r.ui(UiKind.INVOKE, button("SetRateButton", "Set rate"))
            send(pump.SET_RATE, struct.pack("<f", value))
            rate_set = True
        elif action == "volume":
            value = round(rng.uniform(0.5, 50.0), rng.choice([1, 2]))
            r.ui(UiKind.VALUE, vol_box, _fmt(value))
            r.ui(UiKind.INVOKE, button("SetVolumeButton", "Set volume"))
            send(pump.SET_VOLUME, struct.pack("<f", value))
            vol_set = True
        elif action == "direction":
            d = rng.choice([0, 1])
            r.ui(UiKind.SELECTION, dir_sel, pump.DIRECTIONS[d])
            send(pump.SET_DIRECTION, bytes([d]))
        elif action == "start":
            if not (rate_set and vol_set):
                continue
            r.ui(UiKind.INVOKE, button("StartButton", "Start"))
            send(pump.START)
        elif action == "stop":
            r.ui(UiKind.INVOKE, button("StopButton", "Stop"))
            send(pump.STOP)
        elif action == "read":
            r.ui(UiKind.INVOKE, button("ReadRateButton", "Read rate"))
            send(pump.GET_RATE)
    r.wait(rng.uniform(1100, 2000), poll)
    return r.build()


# ----- Modbus RTU temperature controller --------------------------------------------------


def modbus_session(seed: int, framing_mode: str = "rtu") -> Session:
    sim = modbus_tc.ModbusController(framing_mode)
    sim.inputs[0] = random.Random(seed * 7).randint(-60, 40)
    transport = Transport.TCP if framing_mode == "tcp" else Transport.SERIAL
    stream = "tcp:192.168.0.20:502" if framing_mode == "tcp" else "serial:COM3"
    config = None if framing_mode == "tcp" else {**SERIAL_9600, "parity": "E" if framing_mode == "rtu" else "N"}
    r = Recorder(sim, transport, stream, seed=seed, config_meta=config, description=f"modbus {framing_mode}")
    rng = r.rng
    sp_box = edit("SetpointBox", "Setpoint (°C)")
    mode_sel = combo("ModeSelector", "Mode")
    pv_display = display("PvDisplay", "Temperature (°C)")
    transaction = 0

    def send(pdu: bytes) -> bytes:
        nonlocal transaction
        if framing_mode == "ascii":
            raw = r.exchange(modbus_tc.ascii_frame(pdu))
            return bytes.fromhex(raw.strip()[1:].decode())[1:-1] if raw else b""
        if framing_mode == "tcp":
            transaction = (transaction + 1) & 0xFFFF
            raw = r.exchange(modbus_tc.tcp_frame(pdu, transaction))
            return raw[7:]
        raw = r.exchange(modbus_tc.rtu(pdu))
        return raw[1:-2]

    def poll() -> None:
        reply = send(bytes([0x04, 0, 0, 0, 1]))
        if len(reply) >= 4:
            pv = int.from_bytes(reply[2:4], "big", signed=True) / 10
            r.ui(UiKind.VALUE, pv_display, f"{pv:.1f}")

    r.ui(UiKind.INVOKE, button("ConnectButton", "Connect"))
    send(bytes([0x03, 0, 0, 0, 3]))
    actions = ["setpoint", "setpoint", "mode", "mode", "setpoint", "mode"]
    rng.shuffle(actions)
    for action in actions[: rng.randint(4, len(actions))]:
        r.wait(rng.uniform(1100, 2600), poll)
        if action == "setpoint":
            sp = round(rng.uniform(-10, 95), 1)
            r.ui(UiKind.VALUE, sp_box, _fmt(sp))
            r.ui(UiKind.INVOKE, button("ApplyButton", "Apply"))
            send(bytes([0x06, 0, 0]) + (round(sp * 10) & 0xFFFF).to_bytes(2, "big"))
        else:
            mode = rng.choice(list(modbus_tc.MODES))
            r.ui(UiKind.SELECTION, mode_sel, modbus_tc.MODES[mode])
            send(bytes([0x06, 0, 1]) + mode.to_bytes(2, "big"))
    r.wait(rng.uniform(1100, 2000), poll)
    return r.build()


# ----- streaming balance -------------------------------------------------------------------


def balance_session(seed: int) -> Session:
    sim = Balance(seed=seed, realtime=False)
    r = Recorder(
        sim,
        Transport.SERIAL,
        "serial:COM9",
        seed=seed,
        config_meta={**SERIAL_9600, "baudrate": 2400},
        description="balance",
    )
    r.stream_period = 200 * MS
    r._next_stream = r.t + 100 * MS
    rng = r.rng
    actions = ["zero", "tare", "print", "load", "print", "zero", "load", "tare"]
    rng.shuffle(actions)
    for action in actions[: rng.randint(5, len(actions))]:
        r.wait(rng.uniform(900, 2200))
        if action == "zero":
            r.ui(UiKind.INVOKE, button("ZeroButton", "Zero"))
            r.exchange(b"Z\r\n")
        elif action == "tare":
            r.ui(UiKind.INVOKE, button("TareButton", "Tare"))
            r.exchange(b"T\r\n")
        elif action == "print":
            r.ui(UiKind.INVOKE, button("PrintButton", "Print"))
            r.exchange(b"Q\r\n")
        else:
            sim.load(round(rng.uniform(-5, 900), 3))
    r.wait(rng.uniform(900, 1500))
    return r.build()


# ----- HID stirrer ----------------------------------------------------------------------------


def hid_session(seed: int) -> Session:
    sim = hid_stirrer.Stirrer()
    device = hid_stirrer.MockHidDevice(sim)
    device.open(hid_stirrer.VID, hid_stirrer.PID)
    stream = f"hid:\\\\?\\hid#vid_{hid_stirrer.VID:04x}&pid_{hid_stirrer.PID:04x}#1"
    r = Recorder(
        sim,
        Transport.HID,
        stream,
        seed=seed,
        open_meta={"vid": hid_stirrer.VID, "pid": hid_stirrer.PID, "report_size": hid_stirrer.REPORT},
        description="hid stirrer",
    )
    rng = r.rng
    speed_box = edit("SpeedBox", "Speed (rpm)")
    mode_sel = combo("ModeSelector", "Mode")
    speed_display = display("SpeedDisplay", "Actual speed (rpm)")
    seq = rng.randint(0, 255)
    speed_set = False

    def send(cmd: int, *params: int) -> bytes:
        nonlocal seq
        seq = (seq + 1) & 0xFF
        out = hid_stirrer.report(0x01, seq, cmd, *params)
        r.tx(out)
        device.write(out)
        r.t += int(rng.uniform(2, 8) * MS)
        reply = bytes(device.read(64))
        if reply:
            r.rx(reply)
        r.t += int(rng.uniform(1, 4) * MS)
        return reply

    def poll() -> None:
        reply = send(hid_stirrer.GET_SPEED)
        if reply:
            r.ui(UiKind.VALUE, speed_display, str(int.from_bytes(reply[4:6], "little")))

    r.ui(UiKind.INVOKE, button("ConnectButton", "Connect"))
    poll()
    actions = ["speed", "mode", "stop", "mode", "speed"]
    rng.shuffle(actions)
    actions = actions[: rng.randint(3, len(actions))]
    if "speed" not in actions:
        actions.insert(rng.randint(0, len(actions)), "speed")
    actions.insert(rng.randint(actions.index("speed") + 1, len(actions)), "start")
    if rng.random() < 0.6:
        actions.insert(rng.randint(actions.index("start") + 1, len(actions)), "stop")
    for action in actions:
        r.wait(rng.uniform(1100, 2600), poll)
        if action == "speed":
            rpm = rng.randint(30, 1500)
            r.ui(UiKind.VALUE, speed_box, str(rpm))
            r.ui(UiKind.INVOKE, button("ApplyButton", "Apply"))
            send(hid_stirrer.SET_SPEED, rpm & 0xFF, rpm >> 8)
            speed_set = True
        elif action == "mode":
            mode = rng.choice(list(hid_stirrer.MODES))
            r.ui(UiKind.SELECTION, mode_sel, hid_stirrer.MODES[mode])
            send(hid_stirrer.SET_MODE, mode)
        elif action == "start":
            if not speed_set:
                continue
            r.ui(UiKind.INVOKE, button("StartButton", "Start"))
            send(hid_stirrer.START)
        elif action == "stop":
            r.ui(UiKind.INVOKE, button("StopButton", "Stop"))
            send(hid_stirrer.STOP)
    r.wait(rng.uniform(1100, 2000), poll)
    return r.build()


GENERATORS: dict[str, Callable[[int], Session]] = {
    "thermostat": thermostat_session,
    "pump": pump_session,
    "modbus": modbus_session,
    "balance": balance_session,
    "hid": hid_session,
}


def sessions(kind: str, count: int = 10, seed: int = 100) -> list[Session]:
    gen = GENERATORS[kind]
    return [gen(seed + i) for i in range(count)]
