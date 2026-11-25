"""Binary-framed syringe pump.

Frame: STX(0x02) | address | opcode | payload length | payload | CRC-16/IBM-3740
(big-endian) over address..payload. Parameters are little-endian IEEE 754
singles. Every request is answered with opcode | 0x80 and a status byte first.
"""

from __future__ import annotations

import struct

from calder.runtime.checksums import CRC_BY_NAME, crc
from calder.runtime.framing import FramingKind, FramingSpec, LengthField
from tests.simulators.base import Instrument

STX = 0x02
ADDRESS = 0x01
CRC = CRC_BY_NAME["CRC-16/IBM-3740"]

GET_STATUS, SET_RATE, SET_VOLUME, SET_DIRECTION, START, STOP, GET_RATE = 0x01, 0x10, 0x11, 0x12, 0x20, 0x21, 0x30
DIRECTIONS = {0: "Infuse", 1: "Withdraw"}
OK, BAD_LENGTH, OUT_OF_RANGE, NOT_READY, STALLED = 0, 1, 2, 3, 4
RATE_RANGE = (0.001, 50.0)
VOLUME_RANGE = (0.001, 60.0)

GROUND_TRUTH = {
    "start": b"\x02",
    "length": {"offset": 3, "width": 1, "adjust": 6},
    "checksum": {"algorithm": "CRC-16/IBM-3740", "start": 1, "end": -2, "position": -2, "endian": "be"},
    "address": (1, "01"),
    "opcode_offset": 2,
    "tx": {"01", "10", "11", "12", "20", "21", "30"},
    "rx": {"81", "90", "91", "92", "a0", "a1", "b0"},
    "floats": {("tx", "10"): 4, ("tx", "11"): 4, ("rx", "b0"): 5, ("rx", "81"): 6},
    "enums": {("tx", "12"): (4, {"0": "Infuse", "1": "Withdraw"})},
    "status_offset": 4,
    "preconditions": {"20": {"10", "11"}},
}


def frame(opcode: int, payload: bytes = b"", address: int = ADDRESS) -> bytes:
    body = bytes([address, opcode, len(payload)]) + payload
    return bytes([STX]) + body + crc(CRC, body).to_bytes(2, "big")


def parse(data: bytes) -> tuple[int, int, bytes]:
    if len(data) < 6 or data[0] != STX:
        raise ValueError("bad frame")
    if crc(CRC, data[1:-2]) != int.from_bytes(data[-2:], "big"):
        raise ValueError("bad CRC")
    return data[1], data[2], data[4:-2]


class SyringePump(Instrument):
    framing = FramingSpec(FramingKind.LENGTH_PREFIXED, start=b"\x02", length_field=LengthField(3, 1, "le", 6))

    def __init__(self, *, fault: str | None = None) -> None:
        super().__init__()
        self.fault = fault
        self.rate: float | None = None
        self.volume: float | None = None
        self.direction = 0
        self.running = False
        self.dispensed = 0.0

    def advance(self, seconds: float) -> None:
        if self.running and self.rate and self.volume:
            self.dispensed = min(self.volume, self.dispensed + self.rate / 60.0 * seconds)
            if self.dispensed >= self.volume:
                self.running = False

    def respond(self, data: bytes) -> bytes:
        try:
            address, opcode, payload = parse(data)
        except ValueError:
            return b""
        if address != ADDRESS:
            return b""
        reply = opcode | 0x80

        def status(code: int, extra: bytes = b"") -> bytes:
            return frame(reply, bytes([code]) + extra)

        if self.fault == "stall" and opcode == GET_STATUS:
            return status(STALLED, bytes([0]) + struct.pack("<f", self.dispensed))
        if opcode == GET_STATUS:
            return status(OK, bytes([1 if self.running else 0]) + struct.pack("<f", round(self.dispensed, 3)))
        if opcode in (SET_RATE, SET_VOLUME):
            if len(payload) != 4:
                return status(BAD_LENGTH)
            (value,) = struct.unpack("<f", payload)
            lo, hi = RATE_RANGE if opcode == SET_RATE else VOLUME_RANGE
            if not lo <= value <= hi:
                return status(OUT_OF_RANGE)
            if opcode == SET_RATE:
                self.rate = value
            else:
                self.volume = value
            return status(OK)
        if opcode == SET_DIRECTION:
            if len(payload) != 1 or payload[0] not in DIRECTIONS:
                return status(OUT_OF_RANGE)
            self.direction = payload[0]
            return status(OK)
        if opcode == START:
            if self.rate is None or self.volume is None:
                return status(NOT_READY)
            self.running = True
            self.dispensed = 0.0
            return status(OK)
        if opcode == STOP:
            self.running = False
            return status(OK)
        if opcode == GET_RATE:
            return status(OK, struct.pack("<f", self.rate or 0.0))
        return b""
