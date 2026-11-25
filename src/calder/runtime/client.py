"""Request/response client used by generated drivers and by ``calder verify``.

Copied verbatim into generated packages (stdlib only).
"""

from __future__ import annotations

import collections
import time
from collections.abc import Callable, Mapping
from typing import Any

from .framing import Framer, FramingKind, unwrap
from .protocol import Codec, CodecError, Decoded, MessageSpec, ProtocolSpec
from .transport import Transport


class DriverError(RuntimeError):
    """Base class for driver errors."""


class PreconditionError(DriverError):
    """A command was sent before the commands it depends on."""


class ResponseTimeoutError(DriverError):
    """The instrument did not answer in time."""


class UnexpectedResponseError(DriverError):
    """The instrument answered with something other than the expected response."""


class InstrumentStatusError(DriverError):
    """A response carried a status code that was never observed as success."""


ByteLogger = Callable[[str, bytes], None]


class ProtocolClient:
    def __init__(
        self,
        spec: ProtocolSpec,
        transport: Transport,
        *,
        check_preconditions: bool = True,
        timeout_scale: float = 1.0,
        logger: ByteLogger | None = None,
    ) -> None:
        self.spec = spec
        self.codec = Codec(spec)
        self.transport = transport
        self.check_preconditions = check_preconditions
        self.timeout_scale = timeout_scale
        self.logger = logger
        self._enabled: set[str] = set()
        self._framer = Framer(spec.framing, tx=False)
        self._frames: collections.deque[bytes] = collections.deque()
        self.unsolicited: collections.deque[Decoded] = collections.deque(maxlen=1000)

    # ----- preconditions ----------------------------------------------------------

    def unmet_preconditions(self, msg: MessageSpec) -> list[str]:
        return [p.message for p in msg.preconditions if p.message not in self._enabled]

    def _track(self, msg: MessageSpec) -> None:
        self._enabled.add(msg.id)
        for other in self.spec.messages:
            for pre in other.preconditions:
                if msg.id in pre.disabled_by:
                    self._enabled.discard(pre.message)

    # ----- I/O --------------------------------------------------------------------

    def write_frame(self, frame: bytes) -> None:
        if self.logger is not None:
            self.logger("tx", frame)
        self.transport.write(frame)

    def read_frame(self, timeout: float) -> bytes | None:
        if self._frames:
            return self._frames.popleft()
        deadline = time.monotonic() + timeout
        framing = self.spec.framing
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            chunk = self.transport.read(4096, remaining)
            if not chunk:
                continue
            if self.logger is not None:
                self.logger("rx", chunk)
            if self.transport.message_oriented:
                return chunk
            if framing.kind is FramingKind.MESSAGE:
                return self._until_gap(chunk, max(framing.gap_ms, 5.0) / 1000.0)
            frames = self._framer.feed(chunk)
            if frames:
                self._frames.extend(frames[1:])
                return frames[0]

    def _until_gap(self, first: bytes, gap: float) -> bytes:
        """Gap-framed protocols on byte streams: a frame ends at an idle interval."""
        buf = bytearray(first)
        while True:
            more = self.transport.read(4096, gap)
            if not more:
                return bytes(buf)
            if self.logger is not None:
                self.logger("rx", more)
            buf.extend(more)

    def send(
        self,
        message: MessageSpec | str,
        values: Mapping[str, Any] | None = None,
        *,
        check_status: bool = True,
    ) -> Decoded | None:
        msg = self.spec.message(message) if isinstance(message, str) else message
        if self.check_preconditions:
            missing = self.unmet_preconditions(msg)
            if missing:
                names = ", ".join(self.spec.message(m).name for m in missing)
                raise PreconditionError(f"{msg.name} requires {names} first")
        frame = self.codec.encode(msg, values)
        self.write_frame(frame)
        self._track(msg)
        if msg.response is None:
            return None
        return self.expect(
            msg.response,
            timeout=msg.response_timeout_ms / 1000.0 * self.timeout_scale,
            check_status=check_status,
        )

    def expect(self, response_id: str, *, timeout: float, check_status: bool = True) -> Decoded:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            frame = self.read_frame(max(remaining, 0.0))
            if frame is None:
                raise ResponseTimeoutError(f"no {self.spec.message(response_id).name} within {timeout:.2f}s")
            try:
                decoded = self.codec.decode(frame, direction="rx", expected=response_id)
            except CodecError as exc:
                raise UnexpectedResponseError(str(exc)) from exc
            if decoded.id == response_id:
                if check_status:
                    self._check_status(decoded)
                return decoded
            if decoded.message.kind == "unsolicited":
                self.unsolicited.append(decoded)
                continue
            raise UnexpectedResponseError(f"expected {response_id}, got {decoded.id}: {frame!r}")

    def _check_status(self, decoded: Decoded) -> None:
        for f in decoded.message.fields:
            if f.role == "status" and f.status_ok:
                value = decoded.values.get(f.name)
                if str(value) not in f.status_ok:
                    raise InstrumentStatusError(f"{decoded.message.name}.{f.name} = {value!r}")

    def next_unsolicited(self, message_id: str | None = None, *, timeout: float = 1.0) -> Decoded:
        deadline = time.monotonic() + timeout
        while True:
            for item in list(self.unsolicited):
                if message_id is None or item.id == message_id:
                    self.unsolicited.remove(item)
                    return item
            remaining = deadline - time.monotonic()
            frame = self.read_frame(max(remaining, 0.0))
            if frame is None:
                raise ResponseTimeoutError("no unsolicited message")
            decoded = self.codec.decode(frame, direction="rx", expected=message_id)
            if message_id is None or decoded.id == message_id:
                return decoded
            self.unsolicited.append(decoded)

    def text(self, frame: bytes) -> str:
        return unwrap(self.spec.framing, frame, tx=False).decode("latin-1")

    def close(self) -> None:
        self.transport.close()
