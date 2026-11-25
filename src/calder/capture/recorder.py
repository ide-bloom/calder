"""Record a vendor application: wire events from the capture DLL plus UI events."""

from __future__ import annotations

import ctypes
import mmap
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from calder.capture.filters import CaptureFilter
from calder.capture.launcher import Launched, LaunchError, launch, pe_bitness
from calder.capture.ring import HEADER_SIZE, Record, Ring
from calder.capture.uia import UiaRecorder, drive
from calder.session.builder import SessionBuilder
from calder.session.models import AppInfo, Direction, EventKind, Session, Source, Transport

DEFAULT_RING_BYTES = 8 * 1024 * 1024
_VID_PID = re.compile(r"vid_([0-9a-f]{4}).*?pid_([0-9a-f]{4})", re.IGNORECASE)


def _kernel32() -> Any:
    windll = getattr(ctypes, "windll", None)
    return None if windll is None else windll.kernel32


def qpc_now() -> int:
    """QueryPerformanceCounter ticks: the clock the capture DLL and UI helper also use."""
    k32 = _kernel32()
    if k32 is None:
        return time.perf_counter_ns()
    value = ctypes.c_int64()
    k32.QueryPerformanceCounter(ctypes.byref(value))
    return int(value.value)


def qpc_frequency() -> int:
    k32 = _kernel32()
    if k32 is None:
        return 1_000_000_000
    value = ctypes.c_int64()
    k32.QueryPerformanceFrequency(ctypes.byref(value))
    return int(value.value)


def normalize_path(path: str) -> str:
    """``\\\\.\\COM5`` -> ``COM5``; other device paths are kept verbatim."""
    m = re.fullmatch(r"(?:\\\\[.?]\\)?(COM\d+)", path, re.IGNORECASE)
    return m.group(1).upper() if m else path


def identity(transport: str, meta: dict[str, Any], handle: int) -> str:
    if transport in ("serial", "hid"):
        return f"{transport}:{normalize_path(str(meta.get('path', f'handle-{handle:x}')))}"
    if transport == "winusb":
        return f"winusb:{meta.get('path', f'handle-{handle:x}')}"
    if transport == "tcp":
        return f"tcp:{meta.get('remote', f'socket-{handle:x}')}"
    if transport == "visa":
        return f"visa:{meta.get('resource', f'session-{handle:x}')}"
    if transport == "ftdi":
        ident = meta.get("serial_number") or meta.get("description") or meta.get("index", handle)
        return f"ftdi:{ident}"
    return f"{transport}:handle-{handle:x}"


@dataclass
class RecordConverter:
    """Turns raw ring records into session events, tracking handle identities."""

    builder: SessionBuilder
    qpc0: int
    frequency: int
    streams: dict[tuple[int, int], tuple[Transport, str]] = field(default_factory=dict)
    info: list[dict[str, Any]] = field(default_factory=list)

    def t_ns(self, qpc: int) -> int:
        return max(0, (qpc - self.qpc0) * 1_000_000_000 // max(self.frequency, 1))

    def add(self, rec: Record) -> None:
        if rec.kind == "info":
            self.info.append({"qpc": rec.qpc, **rec.meta})
            return
        try:
            transport = Transport(rec.transport)
        except ValueError:
            return
        key = (rec.pid, rec.handle)
        meta = dict(rec.meta)
        if rec.kind == "open":
            ident = identity(rec.transport, meta, rec.handle)
            path = str(meta.get("path", ""))
            m = _VID_PID.search(path)
            if m and "vid" not in meta:
                meta["vid"], meta["pid"] = int(m.group(1), 16), int(m.group(2), 16)
            self.streams[key] = (transport, ident)
        stream_info = self.streams.get(key)
        if stream_info is None or stream_info[0] is not transport:
            # Events on handles opened before capture (or failed calls on invalid
            # handles) get a per-transport placeholder identity.
            stream_info = (transport, f"{rec.transport}:handle-{rec.handle:x}")
        transport, stream = stream_info
        if rec.status:
            meta["status"] = rec.status
        kind = EventKind(rec.kind) if rec.kind in EventKind._value2member_map_ else EventKind.INFO
        self.builder.wire(
            self.t_ns(rec.qpc),
            transport,
            stream,
            Direction(rec.direction),
            rec.data,
            kind,
            pid=rec.pid,
            tid=rec.tid,
            meta=meta,
        )
        if rec.kind == "close":
            self.streams.pop(key, None)


@dataclass
class RecordOptions:
    app: str
    args: list[str] = field(default_factory=list)
    filter: CaptureFilter = field(default_factory=CaptureFilter)
    ui: bool = False
    drive_script: str | None = None
    duration: float | None = None
    ring_bytes: int = DEFAULT_RING_BYTES
    settle: float = 1.5


def record(options: RecordOptions, *, on_status: Callable[[str], None] | None = None) -> Session:
    """Launch, record until the application exits (or ``duration`` elapses), return the session."""
    say = on_status or (lambda _msg: None)
    if sys.platform != "win32":
        raise LaunchError("recording vendor applications needs Windows")
    name = f"Local\\calder-{uuid.uuid4().hex}"
    capacity = options.ring_bytes - options.ring_bytes % 8
    freq = qpc_frequency()
    mm = mmap.mmap(-1, HEADER_SIZE + capacity, tagname=name)
    ring = Ring(mm, initialize=True, capacity=capacity, qpc_frequency=freq)
    app = Path(options.app).resolve()
    builder = SessionBuilder(
        Source.HOOK,
        app=AppInfo(path=str(app), args=options.args, bitness=pe_bitness(app)),
        filter=options.filter.describe(),
    )
    qpc0 = qpc_now()
    conv = RecordConverter(builder, qpc0, freq)
    launched: Launched | None = None
    uia: UiaRecorder | None = None
    driver_error: list[str] = []
    try:
        launched = launch(app, options.args, {**options.filter.environment(), "CALDER_RING": name})
        say(f"launched {app.name} (pid {launched.pid}, {launched.bitness}-bit)")
        if options.ui or options.drive_script:
            uia = UiaRecorder(launched.pid).start()
            for err in uia.errors:
                say(f"UI recorder: {err}")
        driver_thread: threading.Thread | None = None
        if options.drive_script:
            script = options.drive_script

            def run_script(pid: int = launched.pid) -> None:
                time.sleep(options.settle)
                try:
                    drive(pid, script)
                except (LaunchError, OSError) as exc:
                    driver_error.append(str(exc))

            driver_thread = threading.Thread(target=run_script, daemon=True, name="calder-drive")
            driver_thread.start()
        deadline = time.monotonic() + options.duration if options.duration else None
        try:
            while launched.process.poll() is None:
                for rec in ring.drain():
                    conv.add(rec)
                if deadline is not None and time.monotonic() > deadline:
                    say("duration reached; closing the application")
                    launched.terminate()
                    break
                time.sleep(0.01)
        except KeyboardInterrupt:
            say("interrupted; closing the application")
            launched.terminate()
        if driver_thread is not None:
            driver_thread.join(timeout=10)
        time.sleep(0.05)
        for rec in ring.drain():
            conv.add(rec)
    finally:
        if uia is not None:
            uia.stop()
            for ev in uia.events:
                builder.ui(conv.t_ns(ev.qpc), ev.kind, ev.element, ev.value)
        builder.meta.dropped_events = ring.dropped
        ring.release()
        mm.close()
    builder.meta.extra["dll"] = conv.info[:20]
    if driver_error:
        builder.meta.extra["drive_errors"] = driver_error
        raise LaunchError("; ".join(driver_error))
    return builder.build()
