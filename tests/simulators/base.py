"""Shared simulator plumbing: byte-stream framing and servers (TCP, named pipe, serial)."""

from __future__ import annotations

import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

from calder.runtime.framing import Framer, FramingSpec
from calder.runtime.simulator import SimulatorServer


class Instrument(ABC):
    """A simulated instrument: feed host bytes, get instrument bytes back."""

    framing: FramingSpec
    message_oriented = False

    def __init__(self) -> None:
        self._framer = Framer(self.framing, tx=True)
        self.lock = threading.Lock()
        self.received: list[bytes] = []

    def handle(self, data: bytes) -> bytes | list[bytes]:
        with self.lock:
            if self.message_oriented:
                self.received.append(data)
                return self.respond(data)
            out = []
            for frame in self._framer.feed(data):
                self.received.append(frame)
                out.append(self.respond(frame))
            return b"".join(o for o in out if isinstance(o, bytes))

    @abstractmethod
    def respond(self, frame: bytes) -> bytes: ...

    def poll(self) -> bytes:
        """Unsolicited output, if the instrument streams; called periodically."""
        return b""

    def advance(self, seconds: float) -> None:
        """Advance simulated physics (temperatures, volumes)."""
        del seconds


def serve_tcp(instrument: Instrument, host: str = "127.0.0.1", port: int = 0) -> SimulatorServer:
    return SimulatorServer(instrument, host, port, message_mode=instrument.message_oriented).start()


class PipeServer:
    """Byte-mode named pipe server (Windows), so a vendor app can open it with CreateFile."""

    def __init__(self, instrument: Instrument, name: str) -> None:
        if sys.platform != "win32":
            raise OSError("named pipes need Windows")
        self.instrument = instrument
        self.path = name if name.startswith("\\\\.\\pipe\\") else f"\\\\.\\pipe\\{name}"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="calder-pipe")
        self.connections = 0

    def start(self) -> PipeServer:
        self._thread.start()
        return self

    def _run(self) -> None:
        import _winapi

        while not self._stop.is_set():
            # PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT are all zero.
            handle = _winapi.CreateNamedPipe(
                self.path,
                _winapi.PIPE_ACCESS_DUPLEX,
                0,
                1,
                65536,
                65536,
                0,
                _winapi.NULL,
            )
            try:
                try:
                    _winapi.ConnectNamedPipe(handle, False)
                except OSError as exc:
                    if getattr(exc, "winerror", 0) != 535:  # ERROR_PIPE_CONNECTED
                        continue
                if self._stop.is_set():
                    return
                self.connections += 1
                self._serve(handle)
            finally:
                _winapi.CloseHandle(handle)

    def _serve(self, handle: Any) -> None:
        import _winapi

        while not self._stop.is_set():
            try:
                avail, _ = _winapi.PeekNamedPipe(handle, 0)
            except OSError:
                return
            if avail:
                data, err = _winapi.ReadFile(handle, avail, False)
                if err:
                    return
                out = self.instrument.handle(bytes(data))
                if out:
                    _winapi.WriteFile(handle, out if isinstance(out, bytes) else b"".join(out), False)
            else:
                streamed = self.instrument.poll()
                if streamed:
                    try:
                        _winapi.WriteFile(handle, streamed, False)
                    except OSError:
                        return
                time.sleep(0.005)

    def __enter__(self) -> PipeServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        # Unblock ConnectNamedPipe by connecting once.
        try:
            with open(self.path, "r+b", buffering=0):
                pass
        except OSError:
            pass
        self._thread.join(timeout=2.0)


class SerialServer:
    """Serve an instrument on a real (or virtual, e.g. com0com) serial port."""

    def __init__(self, instrument: Instrument, port: str, baudrate: int = 9600) -> None:
        import serial

        self.instrument = instrument
        self._port = serial.Serial(port, baudrate=baudrate, timeout=0.01)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="calder-serial")

    def start(self) -> SerialServer:
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            data = self._port.read(4096)
            if data:
                out = self.instrument.handle(bytes(data))
                if out:
                    self._port.write(out if isinstance(out, bytes) else b"".join(out))
            streamed = self.instrument.poll()
            if streamed:
                self._port.write(streamed)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._port.close()
