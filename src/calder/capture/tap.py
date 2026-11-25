"""Passive serial tap: two receive-only adapters on a Y-cable.

Adapter A listens to the host's TX line (host to instrument), adapter B to
the instrument's TX line (instrument to host). Nothing is ever written.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from calder.session.builder import SessionBuilder
from calder.session.models import Direction, EventKind, Session, Source, Transport


class ReadablePort(Protocol):
    def read(self, size: int = 1) -> bytes: ...

    def close(self) -> None: ...


@dataclass
class TapOptions:
    port_a: str
    port_b: str
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0
    duration: float | None = None


def open_port(name: str, options: TapOptions) -> ReadablePort:
    import serial

    port: Any = serial.Serial(
        name,
        baudrate=options.baudrate,
        bytesize=options.bytesize,
        parity=options.parity,
        stopbits=options.stopbits,
        timeout=0.002,
    )
    return port  # type: ignore[no-any-return]


def tap(
    options: TapOptions,
    *,
    ports: tuple[ReadablePort, ReadablePort] | None = None,
    stop: threading.Event | None = None,
    clock: Callable[[], int] = time.perf_counter_ns,
) -> Session:
    """Record both lines until ``stop`` is set, ``duration`` elapses or Ctrl+C."""
    a, b = ports if ports is not None else (open_port(options.port_a, options), open_port(options.port_b, options))
    stream = f"serial:tap({options.port_a},{options.port_b})"
    builder = SessionBuilder(Source.TAP, description=f"passive tap on {options.port_a} (host) / {options.port_b}")
    t0 = clock()
    lock = threading.Lock()
    done = stop or threading.Event()
    builder.wire(0, Transport.SERIAL, stream, Direction.NONE, b"", EventKind.OPEN, meta={"path": stream, "tap": True})
    builder.wire(
        0,
        Transport.SERIAL,
        stream,
        Direction.NONE,
        b"",
        EventKind.CONFIG,
        meta={
            "baudrate": options.baudrate,
            "bytesize": options.bytesize,
            "parity": options.parity,
            "stopbits": options.stopbits,
        },
    )

    def pump(port: ReadablePort, direction: Direction) -> None:
        while not done.is_set():
            data = port.read(4096)
            if data:
                t = clock() - t0
                with lock:
                    builder.wire(t, Transport.SERIAL, stream, direction, bytes(data))

    threads = [
        threading.Thread(target=pump, args=(a, Direction.TX), daemon=True, name="calder-tap-a"),
        threading.Thread(target=pump, args=(b, Direction.RX), daemon=True, name="calder-tap-b"),
    ]
    for t in threads:
        t.start()
    deadline = time.monotonic() + options.duration if options.duration else None
    try:
        while not done.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        done.set()
        for t in threads:
            t.join(timeout=2)
        a.close()
        b.close()
    builder.wire(clock() - t0, Transport.SERIAL, stream, Direction.NONE, b"", EventKind.CLOSE)
    return builder.build()
