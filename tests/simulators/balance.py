"""Fixed-width ASCII balance that streams stable (ST) and unstable (US) readings.

Stream line: ``ST,+0012.345 g\\r\\n`` (14 characters before CRLF).
Commands: ``Z`` zero -> ``Z A``, ``T`` tare -> ``T A``, ``Q`` -> one reading.
"""

from __future__ import annotations

import random
import time

from calder.runtime.framing import FramingKind, FramingSpec
from tests.simulators.base import Instrument

GROUND_TRUTH = {
    "terminator": b"\r\n",
    "tx": {"Z", "T", "Q"},
    "stream_width": 14,
    "stream_fields": {"state": (0, 2, {"ST", "US"}), "weight": (3, 9, 3)},
}


class Balance(Instrument):
    framing = FramingSpec(FramingKind.DELIMITED, terminator_tx=b"\r\n", terminator_rx=b"\r\n")

    def __init__(self, *, seed: int = 1, interval: float = 0.2, realtime: bool = True) -> None:
        super().__init__()
        self.rng = random.Random(seed)
        self.weight = 12.345
        self.offset = 0.0
        self.unstable = 0
        self.interval = interval
        self.realtime = realtime
        self._last = 0.0

    def load(self, grams: float) -> None:
        self.weight = grams
        self.unstable = 3

    def line(self) -> bytes:
        noise = self.rng.uniform(-0.05, 0.05) if self.unstable else 0.0
        state = "US" if self.unstable else "ST"
        if self.unstable:
            self.unstable -= 1
        value = self.weight - self.offset + noise
        return f"{state},{value:+09.3f} g\r\n".encode("ascii")

    def respond(self, frame: bytes) -> bytes:
        cmd = frame.decode("ascii", "replace").strip().upper()
        if cmd == "Z":
            self.offset = self.weight
            return b"Z A\r\n"
        if cmd == "T":
            self.offset = self.weight
            return b"T A\r\n"
        if cmd == "Q":
            return self.line()
        return b"ES\r\n"

    def poll(self) -> bytes:
        if not self.realtime:
            return self.line()
        now = time.monotonic()
        if now - self._last < self.interval:
            return b""
        self._last = now
        return self.line()
