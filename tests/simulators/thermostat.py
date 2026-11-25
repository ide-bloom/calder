"""SCPI-style thermostat (newline-terminated, IEEE 488.2 common commands).

Set commands require remote mode (``SYST:REM``); ``SYST:LOC`` leaves it.
"""

from __future__ import annotations

from calder.runtime.framing import FramingKind, FramingSpec
from tests.simulators.base import Instrument

IDN = "CALDER,TC-100,SN0001,1.2"
MODES = {"CONST": "Constant", "RAMP": "Ramp", "HOLD": "Hold"}
SETPOINT_RANGE = (-20.0, 150.0)
RATE_RANGE = (0.1, 10.0)

GROUND_TRUTH = {
    "family": "scpi",
    "terminator": b"\n",
    "commands": {
        "*IDN?",
        "SYST:REM",
        "SYST:LOC",
        "SOUR:TEMP",
        "SOUR:TEMP:RATE",
        "SOUR:TEMP?",
        "MEAS:TEMP?",
        "OUTP",
        "MODE",
        "SYST:ERR?",
    },
    "float_args": {"SOUR:TEMP": 2, "SOUR:TEMP:RATE": 2},
    "enums": {"OUTP": {"ON", "OFF"}, "MODE": set(MODES)},
    "enum_labels": {"MODE": MODES},
    "remote_required": {"SOUR:TEMP", "SOUR:TEMP:RATE", "OUTP", "MODE"},
    "remote_on": "SYST:REM",
    "remote_off": "SYST:LOC",
}


class Thermostat(Instrument):
    framing = FramingSpec(FramingKind.DELIMITED, terminator_tx=b"\n", terminator_rx=b"\n")

    def __init__(self, *, fault: str | None = None) -> None:
        super().__init__()
        self.fault = fault
        self.reset()

    def reset(self) -> None:
        self.remote = False
        self.setpoint = 25.0
        self.rate = 1.0
        self.output = False
        self.mode = "CONST"
        self.temperature = 22.0
        self.errors: list[str] = []

    def advance(self, seconds: float) -> None:
        target = self.setpoint if self.output else 22.0
        step = self.rate / 60.0 * seconds * 10
        if abs(target - self.temperature) <= step:
            self.temperature = target
        else:
            self.temperature += step if target > self.temperature else -step

    def _error(self, code: int, text: str) -> None:
        self.errors.append(f'{code},"{text}"')

    def respond(self, frame: bytes) -> bytes:
        line = frame.decode("ascii", "replace").strip()
        reply = self._command(line)
        return (reply + "\n").encode("ascii") if reply is not None else b""

    def _command(self, line: str) -> str | None:
        head, _, arg = line.partition(" ")
        head = head.upper().lstrip(":")
        arg = arg.strip()
        if self.fault == "garbage" and head == "MEAS:TEMP?":
            return "#ERR"
        queries = {
            "*IDN?": lambda: IDN,
            "*OPC?": lambda: "1",
            "SOUR:TEMP?": lambda: f"{self.setpoint:.2f}",
            "SOUR:TEMP:RATE?": lambda: f"{self.rate:.2f}",
            "MEAS:TEMP?": lambda: f"{self.temperature:.2f}",
            "OUTP?": lambda: "1" if self.output else "0",
            "MODE?": lambda: self.mode,
            "SYST:ERR?": lambda: self.errors.pop(0) if self.errors else '0,"No error"',
        }
        if head in queries:
            return queries[head]()
        if head == "*RST":
            self.reset()
            return None
        if head == "*CLS":
            self.errors.clear()
            return None
        if head == "SYST:REM":
            self.remote = True
            return None
        if head == "SYST:LOC":
            self.remote = False
            return None
        setters = {"SOUR:TEMP", "SOUR:TEMP:RATE", "OUTP", "MODE"}
        if head not in setters:
            self._error(-113, "Undefined header")
            return None
        if not self.remote:
            self._error(-221, "Settings conflict; remote mode required")
            return None
        try:
            if head == "SOUR:TEMP":
                value = float(arg)
                if not SETPOINT_RANGE[0] <= value <= SETPOINT_RANGE[1]:
                    self._error(-222, "Data out of range")
                else:
                    self.setpoint = value
            elif head == "SOUR:TEMP:RATE":
                value = float(arg)
                if not RATE_RANGE[0] <= value <= RATE_RANGE[1]:
                    self._error(-222, "Data out of range")
                else:
                    self.rate = value
            elif head == "OUTP":
                if arg.upper() not in ("ON", "OFF", "1", "0"):
                    self._error(-224, "Illegal parameter value")
                else:
                    self.output = arg.upper() in ("ON", "1")
            elif head == "MODE":
                if arg.upper() not in MODES:
                    self._error(-224, "Illegal parameter value")
                else:
                    self.mode = arg.upper()
        except ValueError:
            self._error(-104, "Data type error")
        return None
