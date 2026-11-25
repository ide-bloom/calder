"""HID magnetic stirrer reached through a mock hidapi layer.

Output report (16 bytes): 0x01 | seq | command | parameters | zero padding.
Input report  (16 bytes): 0x02 | seq echo | command | status | value (u16 LE) | padding.
Commands: 0x01 set speed (u16 LE rpm), 0x02 get speed, 0x03 start, 0x04 stop,
0x05 set mode (u8: 0 continuous, 1 interval, 2 reverse).
"""

from __future__ import annotations

import collections

from calder.runtime.framing import FramingKind, FramingSpec
from tests.simulators.base import Instrument

REPORT = 16
SET_SPEED, GET_SPEED, START, STOP, SET_MODE = 0x01, 0x02, 0x03, 0x04, 0x05
MODES = {0: "Continuous", 1: "Interval", 2: "Reverse"}
VID, PID = 0x1D50, 0x6150

GROUND_TRUTH = {
    "framing": "message",
    "counter_offset": 1,
    "echo_offset": 1,
    "opcode_offset": 2,
    "tx": {"01", "02", "03", "04", "05"},
    "speed": ("01", 3, 2, "le"),
    "mode": ("05", 3, {"0": "Continuous", "1": "Interval", "2": "Reverse"}),
    "status_offset": 3,
    "preconditions": {"03": {"01"}},
}


def report(*data: int) -> bytes:
    return bytes(data).ljust(REPORT, b"\x00")


class Stirrer(Instrument):
    framing = FramingSpec(FramingKind.MESSAGE)
    message_oriented = True

    def __init__(self) -> None:
        super().__init__()
        self.speed = 0
        self.mode = 0
        self.running = False

    def respond(self, frame: bytes) -> bytes:
        if len(frame) != REPORT or frame[0] != 0x01:
            return b""
        seq, cmd = frame[1], frame[2]
        status, value = 0, 0
        if cmd == SET_SPEED:
            speed = int.from_bytes(frame[3:5], "little")
            if speed > 2000:
                status = 2
            else:
                self.speed = speed
        elif cmd == GET_SPEED:
            value = self.speed
        elif cmd == START:
            if self.speed == 0:
                status = 3
            else:
                self.running = True
        elif cmd == STOP:
            self.running = False
        elif cmd == SET_MODE:
            if frame[3] not in MODES:
                status = 2
            else:
                self.mode = frame[3]
        else:
            status = 1
        return report(0x02, seq, cmd, status, value & 0xFF, value >> 8)


class MockHidDevice:
    """Duck-types ``hid.device`` (hidapi) on top of a simulator."""

    def __init__(self, instrument: Stirrer) -> None:
        self.instrument = instrument
        self._queue: collections.deque[bytes] = collections.deque()
        self.opened = False

    def open(self, vid: int, pid: int) -> None:
        if (vid, pid) != (VID, PID):
            raise OSError("device not found")
        self.opened = True

    def write(self, data: list[int] | bytes) -> int:
        out = self.instrument.respond(bytes(data))
        if out:
            self._queue.append(out)
        return len(data)

    def read(self, size: int, timeout_ms: int = 0) -> list[int]:
        del timeout_ms
        if not self._queue:
            return []
        return list(self._queue.popleft()[:size])

    def close(self) -> None:
        self.opened = False
