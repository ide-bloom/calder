"""Replay simulator: a virtual instrument built from recorded exchanges.

Responses come from, in order of preference:
1. the recorded response to a byte-identical request;
2. the inferred response template for the request's message type, with
   fields that echo the request or reflect a previously set value updated;
3. nothing, for messages that never had a response.

Copied verbatim into generated packages (stdlib only).
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections import defaultdict
from typing import Any

from .framing import Framer, FramingError, FramingKind
from .protocol import Codec, CodecError, Decoded, ProtocolSpec


class ReplaySimulator:
    def __init__(self, spec: ProtocolSpec, *, stream_interval: float = 0.05) -> None:
        self.spec = spec
        self.codec = Codec(spec)
        self._framer = Framer(spec.framing, tx=True)
        self._exact: dict[bytes, list[bytes]] = defaultdict(list)
        self._templates: dict[str, bytes] = {}
        for ex in spec.exchanges:
            if ex.responses:
                self._exact[ex.request] = list(ex.responses)
                if ex.response_message is not None:
                    self._templates.setdefault(ex.message, ex.responses[0])
        self.state: dict[tuple[str, str], Any] = {}
        self.unknown_frames: list[bytes] = []
        self.stream_interval = stream_interval
        self._stream_pos = 0
        self._last_stream = 0.0
        self._lock = threading.Lock()

    # ----- request handling -------------------------------------------------------

    def handle(self, data: bytes) -> bytes:
        with self._lock:
            if self.spec.framing.kind is FramingKind.MESSAGE:
                frames = [data]
            else:
                try:
                    frames = self._framer.feed(data)
                except FramingError:
                    self._framer.reset()
                    return b""
            return b"".join(self.respond(frame) for frame in frames)

    def respond(self, frame: bytes) -> bytes:
        try:
            request = self.codec.decode(frame, direction="tx")
        except CodecError:
            self.unknown_frames.append(frame)
            return b""
        for f in request.message.fields:
            if f.name in request.values:
                self.state[(request.id, f.id)] = request.values[f.name]
        msg = request.message
        if msg.response is None:
            return b""
        recorded = self._exact.get(frame)
        response_msg = self.spec.message(msg.response)
        template = recorded[-1] if recorded else self._templates.get(msg.id)
        if template is None:
            return b""
        try:
            decoded = self.codec.decode(template, direction="rx", expected=response_msg.id)
        except CodecError:
            return template
        values = self._update(decoded, request)
        if recorded and values == decoded.values:
            return template
        try:
            return self.codec.encode(response_msg, values)
        except CodecError:
            return template

    def _update(self, response: Decoded, request: Decoded) -> dict[str, Any]:
        values = dict(response.values)
        for f in response.message.fields:
            if f.echo_of is not None:
                for rf in request.message.fields:
                    if rf.id == f.echo_of and rf.name in request.values:
                        values[f.name] = request.values[rf.name]
            if f.reflects is not None and f.reflects in self.state:
                values[f.name] = self.state[f.reflects]
        return values

    # ----- unsolicited stream -----------------------------------------------------

    def poll(self) -> bytes:
        """Next streamed (unsolicited) frame, paced by ``stream_interval``."""
        if not self.spec.unsolicited:
            return b""
        now = time.monotonic()
        if now - self._last_stream < self.stream_interval:
            return b""
        self._last_stream = now
        frame = self.spec.unsolicited[self._stream_pos % len(self.spec.unsolicited)]
        self._stream_pos += 1
        return frame


class SimulatorServer:
    """Serve a simulator over TCP. Message-oriented protocols use 2-byte length prefixes."""

    def __init__(self, simulator: Any, host: str = "127.0.0.1", port: int = 0, *, message_mode: bool = False):
        self.simulator = simulator
        self.message_mode = message_mode
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(4)
        self.host, self.port = self._sock.getsockname()[:2]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self) -> SimulatorServer:
        t = threading.Thread(target=self._accept, daemon=True, name="calder-sim-accept")
        t.start()
        self._threads.append(t)
        return self

    def _accept(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True, name="calder-sim-conn")
            t.start()
            self._threads.append(t)

    def _send(self, conn: socket.socket, data: bytes) -> None:
        if self.message_mode:
            conn.sendall(struct.pack(">H", len(data)) + data)
        else:
            conn.sendall(data)

    def _serve(self, conn: socket.socket) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(0.02)
        buf = bytearray()
        poll = getattr(self.simulator, "poll", None)
        with conn:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                except TimeoutError:
                    chunk = b""
                except OSError:
                    return
                try:
                    if self.message_mode:
                        buf.extend(chunk)
                        while len(buf) >= 2 and len(buf) >= 2 + struct.unpack(">H", buf[:2])[0]:
                            (n,) = struct.unpack(">H", buf[:2])
                            message = bytes(buf[2 : 2 + n])
                            del buf[: 2 + n]
                            out = self.simulator.handle(message)
                            for item in out if isinstance(out, list) else [out]:
                                if item:
                                    self._send(conn, bytes(item))
                    elif chunk:
                        out = self.simulator.handle(chunk)
                        if out:
                            self._send(conn, bytes(out))
                    if poll is not None:
                        streamed = poll()
                        if streamed:
                            self._send(conn, bytes(streamed))
                except OSError:
                    return

    def stop(self) -> None:
        self._stop.set()
        self._sock.close()
        for t in self._threads:
            t.join(timeout=1.0)

    def __enter__(self) -> SimulatorServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
