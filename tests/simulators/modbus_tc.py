"""Modbus temperature controller (RTU by default; ASCII and TCP framings for recognition tests).

Registers (zero-based):
  holding 0  setpoint, int16, 0.1 °C
  holding 1  mode, 0 = Off, 1 = Auto, 2 = Manual
  holding 2  output limit, uint16, %
  input 0    process value, int16, 0.1 °C
"""

from __future__ import annotations

from calder.runtime.checksums import CRC_BY_NAME, crc, sum8_twos
from calder.runtime.framing import FramingKind, FramingSpec, LengthField
from tests.simulators.base import Instrument

UNIT = 0x11
CRC = CRC_BY_NAME["CRC-16/MODBUS"]
MODES = {0: "Off", 1: "Auto", 2: "Manual"}

GROUND_TRUTH = {
    "family": "modbus_rtu",
    "checksum": {"algorithm": "CRC-16/MODBUS", "start": 0, "end": -2, "position": -2, "endian": "le"},
    "tx": {"03:0000x3", "04:0000x1", "06:0000", "06:0001"},
    "setpoint": ("06:0000", 4, "int", "be", -1),
    "pv": ("response:04:0000x1", 3, "int", "be", -1),
    "mode": ("06:0001", 4, {"0": "Off", "1": "Auto", "2": "Manual"}),
}


def rtu(pdu: bytes, unit: int = UNIT) -> bytes:
    body = bytes([unit]) + pdu
    return body + crc(CRC, body).to_bytes(2, "little")


def ascii_frame(pdu: bytes, unit: int = UNIT) -> bytes:
    body = bytes([unit]) + pdu
    return b":" + (body + bytes([sum8_twos(body)])).hex().upper().encode() + b"\r\n"


def tcp_frame(pdu: bytes, transaction: int, unit: int = UNIT) -> bytes:
    return transaction.to_bytes(2, "big") + b"\x00\x00" + (len(pdu) + 1).to_bytes(2, "big") + bytes([unit]) + pdu


class ModbusController(Instrument):
    framing = FramingSpec(FramingKind.MODBUS_RTU)

    def __init__(self, framing_mode: str = "rtu") -> None:
        self.mode_name = framing_mode
        if framing_mode == "ascii":
            self.framing = FramingSpec(FramingKind.DELIMITED, terminator_tx=b"\r\n", terminator_rx=b"\r\n")
        elif framing_mode == "tcp":
            self.framing = FramingSpec(FramingKind.LENGTH_PREFIXED, length_field=LengthField(4, 2, "be", 6))
        super().__init__()
        self.holding = [250, 1, 100]
        self.inputs = [221]

    def advance(self, seconds: float) -> None:
        target = 220 if self.holding[1] == 0 else self.holding[0]
        step = max(1, int(seconds * 5))
        pv = self.inputs[0]
        self.inputs[0] = target if abs(target - pv) <= step else pv + (step if target > pv else -step)

    def respond(self, data: bytes) -> bytes:
        if self.mode_name == "ascii":
            raw = bytes.fromhex(data.strip()[1:].decode())
            unit, pdu = raw[0], raw[1:-1]
        elif self.mode_name == "tcp":
            unit, pdu = data[6], data[7:]
        else:
            if crc(CRC, data[:-2]) != int.from_bytes(data[-2:], "little"):
                return b""
            unit, pdu = data[0], data[1:-2]
        if unit != UNIT:
            return b""
        out = self._pdu(pdu)
        if self.mode_name == "ascii":
            return ascii_frame(out)
        if self.mode_name == "tcp":
            return tcp_frame(out, int.from_bytes(data[:2], "big"))
        return rtu(out)

    def _pdu(self, pdu: bytes) -> bytes:
        func = pdu[0]

        def exception(code: int) -> bytes:
            return bytes([func | 0x80, code])

        def s16(v: int) -> bytes:
            return (v & 0xFFFF).to_bytes(2, "big")

        if func in (0x03, 0x04):
            start = int.from_bytes(pdu[1:3], "big")
            count = int.from_bytes(pdu[3:5], "big")
            table = self.holding if func == 0x03 else self.inputs
            if start + count > len(table) or count == 0:
                return exception(0x02)
            values = b"".join(s16(v) for v in table[start : start + count])
            return bytes([func, len(values)]) + values
        if func == 0x06:
            reg = int.from_bytes(pdu[1:3], "big")
            value = int.from_bytes(pdu[3:5], "big", signed=reg == 0)
            if reg >= len(self.holding):
                return exception(0x02)
            if reg == 1 and value not in MODES:
                return exception(0x03)
            self.holding[reg] = value
            return pdu[:5]
        if func == 0x10:
            start = int.from_bytes(pdu[1:3], "big")
            count = int.from_bytes(pdu[3:5], "big")
            if start + count > len(self.holding):
                return exception(0x02)
            for i in range(count):
                self.holding[start + i] = int.from_bytes(pdu[6 + 2 * i : 8 + 2 * i], "big", signed=start + i == 0)
            return pdu[:5]
        return exception(0x01)
