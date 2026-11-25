"""Live verification of a generated driver inside a strict safety envelope.

Rules (docs/safety.md):

* ``read``: only byte-identical recorded requests of read-only messages are sent.
* ``replay``: requires ``allow_actuation`` and a confirmation per command class;
  replays one recorded session's requests byte for byte.
* ``explore``: requires ``allow_actuation``, a confirmation per class and a
  limits file; varies numeric parameters only inside the limits, which must lie
  within the observed range unless a field explicitly opts out, and enums only
  over observed values.
* Never: a message type (opcode or mnemonic) that was not observed.
* Always: a rate limit, immediate abort on an unexpected response, an error
  status or a timeout, and a log of every transmitted and received byte.
"""

from __future__ import annotations

import json
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

from calder.runtime.client import (
    DriverError,
    InstrumentStatusError,
    ProtocolClient,
    ResponseTimeoutError,
    UnexpectedResponseError,
)
from calder.runtime.encodings import EncodingKind
from calder.runtime.protocol import Codec, CodecError, Decoded, MessageSpec, ProtocolSpec
from calder.runtime.transport import Transport

WARNING = (
    "Calder is about to send commands to a physical instrument. Instruments can heat, move, "
    "dispense, pressurize or apply high voltage. Be present at the instrument with its own "
    "emergency stop within reach. Calder aborts on any unexpected response, but it cannot "
    "know what the instrument does physically."
)
MAX_RATE_HZ = 20.0
DEFAULT_RATE_HZ = 2.0


class Phase(StrEnum):
    READ = "read"
    REPLAY = "replay"
    EXPLORE = "explore"


class SafetyError(RuntimeError):
    """Calder refused to send something; nothing was transmitted for it."""


class VerificationAborted(RuntimeError):  # noqa: N818 - an outcome, not a programming error
    """The instrument answered unexpectedly; verification stopped immediately."""


@dataclass(frozen=True)
class FieldLimit:
    minimum: float
    maximum: float
    beyond_observed: bool = False


@dataclass
class Limits:
    commands: dict[str, dict[str, FieldLimit]] = field(default_factory=dict)
    max_commands: int = 100
    rate_hz: float | None = None

    @classmethod
    def load(cls, path: str | Path) -> Limits:
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        data: dict[str, Any] = tomllib.loads(text) if p.suffix.lower() == ".toml" else json.loads(text)
        commands: dict[str, dict[str, FieldLimit]] = {}
        for cmd, fields in (data.get("commands") or {}).items():
            commands[str(cmd)] = {}
            for name, lim in fields.items():
                lo, hi = float(lim["min"]), float(lim["max"])
                if lo > hi:
                    raise SafetyError(f"limits for {cmd}.{name}: min > max")
                commands[str(cmd)][str(name)] = FieldLimit(lo, hi, bool(lim.get("beyond_observed", False)))
        return cls(commands, int(data.get("max_commands", 100)), data.get("rate_hz"))


@dataclass
class Step:
    message: str
    request: str
    response: str | None
    ok: bool
    note: str = ""


@dataclass
class VerifyResult:
    phase: Phase
    steps: list[Step] = field(default_factory=list)
    aborted: bool = False
    reason: str = ""
    log_path: str = ""

    @property
    def sent(self) -> int:
        return len(self.steps)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.steps if s.ok)


class ByteLog:
    """JSON Lines log of every byte sent and received."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._fh: TextIO | None = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        self.entries: list[dict[str, str]] = []

    def __call__(self, direction: str, data: bytes) -> None:
        entry = {"time": datetime.now(UTC).isoformat(), "dir": direction, "hex": data.hex()}
        self.entries.append(entry)
        if self._fh is not None:
            self._fh.write(json.dumps(entry) + "\n")
            self._fh.flush()

    def note(self, text: str) -> None:
        entry = {"time": datetime.now(UTC).isoformat(), "dir": "note", "text": text}
        self.entries.append(entry)
        if self._fh is not None:
            self._fh.write(json.dumps(entry) + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class SafetyGuard:
    """Decides whether a frame may be transmitted in the current phase."""

    def __init__(self, spec: ProtocolSpec, phase: Phase, limits: Limits | None = None) -> None:
        self.spec = spec
        self.phase = phase
        self.limits = limits
        self.codec = Codec(spec)
        self.recorded = {ex.request for ex in spec.exchanges}
        self.tx_ids = {m.id for m in spec.tx_messages()}

    def identify(self, frame: bytes) -> Decoded:
        try:
            decoded = self.codec.decode(frame, direction="tx")
        except CodecError as exc:
            raise SafetyError(f"refusing to send an unobserved command: {frame!r}") from exc
        if decoded.id not in self.tx_ids:
            raise SafetyError(f"refusing to send unobserved message {decoded.id}")
        return decoded

    def check(self, frame: bytes) -> Decoded:
        decoded = self.identify(frame)
        msg = decoded.message
        if self.phase is Phase.READ:
            if msg.classification != "read_only" or frame not in self.recorded:
                raise SafetyError(f"phase 'read' only sends recorded read-only queries, not {msg.name}")
        elif self.phase is Phase.REPLAY:
            if frame not in self.recorded:
                raise SafetyError(f"phase 'replay' only sends recorded frames; {msg.name} frame was never recorded")
        else:
            self._check_explore(decoded)
        return decoded

    def _recorded_values(self, msg: MessageSpec, name: str) -> list[Any]:
        values: list[Any] = []
        for ex in self.spec.exchanges:
            if ex.message == msg.id:
                try:
                    values.append(self.codec.decode_as(msg, ex.request).values.get(name))
                except CodecError:
                    continue
        return values

    def _check_explore(self, decoded: Decoded) -> None:
        msg = decoded.message
        if decoded.raw in self.recorded:
            return
        if self.limits is None:
            raise SafetyError("phase 'explore' requires a limits file")
        allowed = self.limits.commands.get(msg.name)
        if allowed is None:
            raise SafetyError(f"{msg.name} is not listed in the limits file")
        for f in msg.fields:
            if f.name not in decoded.values or not f.is_input:
                continue
            value = decoded.values[f.name]
            if f.role == "enum" or (f.encoding is not None and f.encoding.kind is EncodingKind.ASCII_ENUM):
                if f.enum and str(value) not in f.enum:
                    raise SafetyError(f"{msg.name}.{f.name}={value!r} was never observed")
                continue
            lim = allowed.get(f.name)
            if lim is None:
                # Fields without limits may only keep a value that was recorded.
                if value not in self._recorded_values(msg, f.name):
                    raise SafetyError(f"{msg.name}.{f.name}={value!r} has no limit and was never recorded")
                continue
            if not isinstance(value, int | float):
                raise SafetyError(f"{msg.name}.{f.name} is not numeric")
            if not lim.minimum <= float(value) <= lim.maximum:
                raise SafetyError(f"{msg.name}.{f.name}={value} is outside the limits [{lim.minimum}, {lim.maximum}]")
            if (
                not lim.beyond_observed
                and f.minimum is not None
                and f.maximum is not None
                and not (f.minimum <= float(value) <= f.maximum)
            ):
                raise SafetyError(
                    f"{msg.name}.{f.name}={value} is outside the observed range [{f.minimum}, {f.maximum}]"
                )


Confirm = Callable[[str, list[str]], bool]
Warn = Callable[[str], None]


def _deny(_cls: str, _names: list[str]) -> bool:
    return False


class Verifier:
    def __init__(
        self,
        spec: ProtocolSpec,
        transport: Transport,
        *,
        phase: Phase,
        allow_actuation: bool = False,
        limits: Limits | None = None,
        confirm: Confirm = _deny,
        warn: Warn | None = None,
        rate_hz: float = DEFAULT_RATE_HZ,
        log_path: str | Path | None = None,
        session: str | None = None,
        timeout_scale: float = 1.0,
    ) -> None:
        self.spec = spec
        self.phase = Phase(phase)
        self.allow_actuation = allow_actuation
        self.limits = limits
        self.confirm = confirm
        self.warn = warn or (lambda _text: None)
        rate = limits.rate_hz if limits and limits.rate_hz else rate_hz
        if rate <= 0:
            raise SafetyError("rate limit must be positive")
        self.interval = 1.0 / min(rate, MAX_RATE_HZ)
        self.log = ByteLog(log_path)
        self.guard = SafetyGuard(spec, self.phase, limits)
        self.client = ProtocolClient(
            spec, transport, check_preconditions=False, logger=self.log, timeout_scale=timeout_scale
        )
        self.session = session
        self._last_tx = 0.0
        self.error_query = self._error_query()

    def _error_query(self) -> bytes | None:
        """A recorded SCPI-style error query whose response carries a status field."""
        for ex in self.spec.exchanges:
            msg = self.spec.message(ex.message)
            if msg.classification != "read_only" or not msg.response:
                continue
            resp = self.spec.message(msg.response)
            if any(f.role == "status" for f in resp.fields) and msg.key.upper().endswith("ERR?"):
                return ex.request
        return None

    # ----- planning -----------------------------------------------------------------

    def plan(self) -> list[bytes]:
        if self.phase is Phase.READ:
            seen: set[bytes] = set()
            out: list[bytes] = []
            for ex in self.spec.exchanges:
                msg = self.spec.message(ex.message)
                if msg.classification == "read_only" and not msg.preconditions and ex.request not in seen:
                    seen.add(ex.request)
                    out.append(ex.request)
            return out
        if not self.allow_actuation:
            raise SafetyError(f"phase '{self.phase.value}' requires --allow-actuation")
        if self.phase is Phase.REPLAY:
            sessions: dict[str, list[bytes]] = {}
            for ex in self.spec.exchanges:
                sessions.setdefault(ex.session, []).append(ex.request)
            if not sessions:
                raise SafetyError("the model has no recorded exchanges to replay")
            if self.session is not None:
                if self.session not in sessions:
                    raise SafetyError(f"no recorded session {self.session!r}")
                return sessions[self.session]
            return max(sessions.values(), key=len)
        return self._explore_plan()

    def _explore_plan(self) -> list[bytes]:
        if self.limits is None:
            raise SafetyError("phase 'explore' requires a limits file (--limits)")
        codec = Codec(self.spec)
        plan: list[bytes] = []
        for name in self.limits.commands:
            try:
                msg = self.spec.by_name(name)
            except KeyError as exc:
                raise SafetyError(f"limits name an unobserved command {name!r}") from exc
            for pre in msg.preconditions:
                plan.append(self._recorded_request(pre.message))
            base = self._recorded_values(msg)
            for variant in self._variants(msg):
                values = {**base, **variant}
                plan.append(codec.encode(msg, values))
            if len(plan) >= self.limits.max_commands:
                break
        return plan[: self.limits.max_commands]

    def _recorded_request(self, message_id: str) -> bytes:
        for ex in self.spec.exchanges:
            if ex.message == message_id:
                return ex.request
        raise SafetyError(f"precondition {message_id} has no recorded request")

    def _recorded_values(self, msg: MessageSpec) -> dict[str, Any]:
        codec = Codec(self.spec)
        frame = self._recorded_request(msg.id)
        return dict(codec.decode_as(msg, frame).values)

    def _variants(self, msg: MessageSpec) -> list[dict[str, Any]]:
        assert self.limits is not None
        limits = self.limits.commands.get(msg.name, {})
        out: list[dict[str, Any]] = []
        for f in msg.inputs:
            if f.role == "enum":
                out.extend({f.name: _wire_value(f.encoding, wire)} for wire in f.enum)
                continue
            lim = limits.get(f.name)
            if lim is None:
                continue
            lo, hi = lim.minimum, lim.maximum
            if not lim.beyond_observed and f.minimum is not None and f.maximum is not None:
                lo, hi = max(lo, f.minimum), min(hi, f.maximum)
                if lo > hi:
                    raise SafetyError(f"limits for {msg.name}.{f.name} do not overlap the observed range")
            integral = (
                f.encoding is not None
                and f.encoding.kind.value in ("uint", "int", "ascii_int", "bcd")
                and f.encoding.scale_exp >= 0
            )
            for v in (lo, (lo + hi) / 2, hi):
                out.append({f.name: round(v) if integral else v})
        return out

    # ----- execution ----------------------------------------------------------------

    def _confirm_classes(self, plan: list[bytes]) -> None:
        classes: dict[str, set[str]] = {}
        for frame in plan:
            msg = self.guard.identify(frame).message
            if msg.classification != "read_only":
                classes.setdefault(msg.classification, set()).add(msg.name)
        for cls in ("configuring", "actuating"):
            if cls in classes and not self.confirm(cls, sorted(classes[cls])):
                raise SafetyError(f"{cls} commands were not confirmed; nothing was sent")

    def _pace(self) -> None:
        wait = self._last_tx + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_tx = time.monotonic()

    def _exchange(self, frame: bytes, result: VerifyResult) -> None:
        decoded = self.guard.check(frame)
        msg = decoded.message
        self._pace()
        self.client.write_frame(frame)
        if msg.response is None:
            result.steps.append(Step(msg.name, frame.hex(), None, True, "no response expected"))
            return
        try:
            reply = self.client.expect(
                msg.response, timeout=msg.response_timeout_ms / 1000.0 * self.client.timeout_scale
            )
        except ResponseTimeoutError as exc:
            result.steps.append(Step(msg.name, frame.hex(), None, False, str(exc)))
            raise VerificationAborted(f"timeout waiting for {msg.name}: {exc}") from exc
        except (UnexpectedResponseError, InstrumentStatusError) as exc:
            result.steps.append(Step(msg.name, frame.hex(), None, False, str(exc)))
            raise VerificationAborted(f"{msg.name}: {exc}") from exc
        self._compare(msg, reply, frame)
        result.steps.append(Step(msg.name, frame.hex(), reply.raw.hex(), True))

    def _compare(self, msg: MessageSpec, reply: Decoded, request: bytes) -> None:
        """Structural comparison: same response type, enum values observed, echoes match."""
        req = self.guard.codec.decode(request, direction="tx", expected=msg.id)
        for f in reply.message.fields:
            if f.echo_of is not None:
                source = next((x for x in msg.fields if x.id == f.echo_of), None)
                if (
                    source is not None
                    and source.name in req.values
                    and req.values[source.name] != reply.values.get(f.name)
                ):
                    raise VerificationAborted(f"{reply.message.name}.{f.name} does not echo the request")

    def _check_errors(self, result: VerifyResult) -> None:
        if self.error_query is None:
            return
        frame = self.error_query
        msg = self.guard.identify(frame).message
        self._pace()
        self.client.write_frame(frame)
        assert msg.response is not None
        try:
            reply = self.client.expect(msg.response, timeout=msg.response_timeout_ms / 1000.0)
        except (DriverError, CodecError) as exc:
            raise VerificationAborted(f"error query failed: {exc}") from exc
        result.steps.append(Step(msg.name, frame.hex(), reply.raw.hex(), True, "error queue empty"))

    def run(self) -> VerifyResult:
        result = VerifyResult(self.phase, log_path=str(self.log.path or ""))
        self.warn(WARNING)
        self.log.note(f"verify phase={self.phase.value} protocol={self.spec.name} model={self.spec.model_sha256}")
        try:
            plan = self.plan()
            for frame in plan:
                self.guard.check(frame)
            if self.phase is not Phase.READ:
                self._confirm_classes(plan)
            for frame in plan:
                self._exchange(frame, result)
                msg = self.guard.identify(frame).message
                if self.phase is not Phase.READ and msg.classification != "read_only":
                    self._check_errors(result)
        except VerificationAborted as exc:
            result.aborted = True
            result.reason = str(exc)
            self.log.note(f"aborted: {exc}")
        except InstrumentStatusError as exc:
            result.aborted = True
            result.reason = f"error status: {exc}"
            self.log.note(result.reason)
        finally:
            self.log.close()
        return result


def _wire_value(encoding: Any, wire: str) -> Any:
    if encoding is not None and encoding.kind.value in ("uint", "int", "bcd", "bits"):
        return int(wire)
    return wire


def find_protocol(driver_dir: str | Path) -> Path:
    root = Path(driver_dir)
    if root.is_file() and root.name == "protocol.json":
        return root
    candidates = [p for p in root.rglob("protocol.json") if "tests" not in p.parts]
    if not candidates:
        raise FileNotFoundError(f"no protocol.json under {root}")
    return sorted(candidates, key=lambda p: len(p.parts))[0]
