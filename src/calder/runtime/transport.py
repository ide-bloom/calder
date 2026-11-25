"""Transports for generated drivers.

Optional third-party libraries (pyserial, pyvisa, pyusb, hidapi) are imported
lazily so a driver only needs the library for the transport it actually uses.

Copied verbatim into generated packages (stdlib only at import time).
"""

from __future__ import annotations

import ctypes
import importlib
import queue
import socket
import struct
import sys
import time
from collections.abc import Callable
from typing import Any, Protocol


class TransportError(OSError):
    """The instrument connection failed."""


class Transport(Protocol):
    message_oriented: bool

    def write(self, data: bytes) -> None: ...

    def read(self, size: int, timeout: float) -> bytes:
        """Return available bytes (at most ``size``) or ``b""`` after ``timeout`` seconds."""
        ...

    def close(self) -> None: ...


def _import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise TransportError(f"this transport needs the '{name}' package: pip install {name}") from exc


PARITY = {"N": "N", "E": "E", "O": "O", "M": "M", "S": "S"}


class SerialTransport:
    """pyserial; also accepts pyserial URLs such as ``socket://host:port``."""

    message_oriented = False

    def __init__(self, resource: str, settings: dict[str, Any] | None = None) -> None:
        serial = _import("serial")
        cfg = settings or {}
        self._port = serial.serial_for_url(
            resource,
            baudrate=int(cfg.get("baudrate") or 9600),
            bytesize=int(cfg.get("bytesize") or 8),
            parity=PARITY.get(str(cfg.get("parity") or "N"), "N"),
            stopbits=float(cfg.get("stopbits") or 1),
            xonxoff=bool(cfg.get("xonxoff")),
            rtscts=bool(cfg.get("rtscts")),
            dsrdtr=bool(cfg.get("dsrdtr")),
            timeout=0.1,
            do_not_open=False,
        )

    def write(self, data: bytes) -> None:
        self._port.write(data)
        self._port.flush()

    def read(self, size: int, timeout: float) -> bytes:
        self._port.timeout = timeout
        first = bytes(self._port.read(1))
        if not first:
            return b""
        self._port.timeout = 0
        waiting = max(0, min(size - 1, int(getattr(self._port, "in_waiting", 0) or 0)))
        return first + (bytes(self._port.read(waiting)) if waiting else b"")

    def close(self) -> None:
        self._port.close()


class SocketTransport:
    """TCP. In message mode each message is wrapped in a 2-byte big-endian length.

    Message mode carries HID or USB reports, which have boundaries a byte stream
    would lose, between a driver and a simulator.
    """

    def __init__(self, host: str, port: int, *, message_mode: bool = False, timeout: float = 5.0) -> None:
        self.message_oriented = message_mode
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise TransportError(f"cannot connect to {host}:{port}: {exc}") from exc
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        payload = struct.pack(">H", len(data)) + data if self.message_oriented else data
        self._sock.sendall(payload)

    def _recv(self, timeout: float) -> bytes:
        self._sock.settimeout(max(timeout, 0.001))
        try:
            chunk = self._sock.recv(65536)
        except TimeoutError:
            return b""
        if not chunk:
            raise TransportError("connection closed by peer")
        return chunk

    def read(self, size: int, timeout: float) -> bytes:
        if not self.message_oriented:
            return self._recv(timeout)[:size] if not self._buf else self._take(size)
        deadline = time.monotonic() + timeout
        while True:
            if len(self._buf) >= 2:
                (length,) = struct.unpack(">H", self._buf[:2])
                if len(self._buf) >= 2 + length:
                    msg = bytes(self._buf[2 : 2 + length])
                    del self._buf[: 2 + length]
                    return msg
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b""
            self._buf.extend(self._recv(remaining))

    def _take(self, size: int) -> bytes:
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def close(self) -> None:
        self._sock.close()


class VisaTransport:
    """pyvisa raw I/O. Message-based resources return one message per read."""

    message_oriented = False

    def __init__(self, resource: str, settings: dict[str, Any] | None = None) -> None:
        pyvisa = _import("pyvisa")
        cfg = settings or {}
        self._rm = pyvisa.ResourceManager(cfg.get("library", ""))
        self._inst = self._rm.open_resource(resource)
        self._inst.read_termination = None
        self._inst.write_termination = None
        if "baudrate" in cfg and hasattr(self._inst, "baud_rate"):
            self._inst.baud_rate = int(cfg["baudrate"])

    def write(self, data: bytes) -> None:
        self._inst.write_raw(data)

    def read(self, size: int, timeout: float) -> bytes:
        self._inst.timeout = int(timeout * 1000)
        try:
            return bytes(self._inst.read_raw(size))
        except Exception as exc:
            if "Timeout" in type(exc).__name__ or "TMO" in str(exc):
                return b""
            raise TransportError(str(exc)) from exc

    def close(self) -> None:
        self._inst.close()
        self._rm.close()


class FtdiTransport:
    """FTDI D2XX through ctypes, so no extra Python package is required."""

    message_oriented = False
    FT_OPEN_BY_SERIAL_NUMBER = 1
    FT_OPEN_BY_DESCRIPTION = 2

    def __init__(self, resource: str, settings: dict[str, Any] | None = None) -> None:
        cfg = settings or {}
        if sys.platform == "win32":
            self._lib: Any = ctypes.WinDLL("ftd2xx.dll")  # type: ignore[attr-defined,unused-ignore]
        else:  # pragma: no cover - Linux/macOS D2XX builds
            self._lib = ctypes.CDLL("libftd2xx.so")
        self._handle = ctypes.c_void_p()
        how = self.FT_OPEN_BY_DESCRIPTION if resource.startswith("desc:") else self.FT_OPEN_BY_SERIAL_NUMBER
        name = resource.removeprefix("desc:").removeprefix("sn:").encode()
        self._check(self._lib.FT_OpenEx(ctypes.c_char_p(name), how, ctypes.byref(self._handle)), "FT_OpenEx")
        self._check(self._lib.FT_SetBaudRate(self._handle, int(cfg.get("baudrate") or 9600)), "FT_SetBaudRate")
        parity = {"N": 0, "O": 1, "E": 2, "M": 3, "S": 4}[str(cfg.get("parity") or "N")]
        stop = 2 if float(cfg.get("stopbits") or 1) == 2 else 0
        self._check(
            self._lib.FT_SetDataCharacteristics(self._handle, int(cfg.get("bytesize") or 8), stop, parity),
            "FT_SetDataCharacteristics",
        )
        flow = 0x0100 if cfg.get("rtscts") else 0x0400 if cfg.get("xonxoff") else 0
        self._check(self._lib.FT_SetFlowControl(self._handle, flow, 0x11, 0x13), "FT_SetFlowControl")

    @staticmethod
    def _check(status: int, what: str) -> None:
        if status != 0:
            raise TransportError(f"{what} failed with FT_STATUS {status}")

    def write(self, data: bytes) -> None:
        written = ctypes.c_uint32()
        buf = ctypes.create_string_buffer(data, len(data))
        self._check(self._lib.FT_Write(self._handle, buf, len(data), ctypes.byref(written)), "FT_Write")

    def read(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        queued = ctypes.c_uint32()
        while True:
            self._check(self._lib.FT_GetQueueStatus(self._handle, ctypes.byref(queued)), "FT_GetQueueStatus")
            if queued.value:
                n = min(size, queued.value)
                buf = ctypes.create_string_buffer(n)
                got = ctypes.c_uint32()
                self._check(self._lib.FT_Read(self._handle, buf, n, ctypes.byref(got)), "FT_Read")
                return buf.raw[: got.value]
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.002)

    def close(self) -> None:
        self._lib.FT_Close(self._handle)


class UsbTransport:
    """pyusb bulk or interrupt endpoints (WinUSB devices)."""

    message_oriented = True

    def __init__(self, vid: int, pid: int, settings: dict[str, Any] | None = None) -> None:
        usb_core = _import("usb.core")
        cfg = settings or {}
        dev = usb_core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise TransportError(f"USB device {vid:04x}:{pid:04x} not found")
        dev.set_configuration()
        self._dev = dev
        self._ep_out = int(cfg.get("endpoint_out", 0x01))
        self._ep_in = int(cfg.get("endpoint_in", 0x81))
        self._interface = int(cfg.get("interface", 0))

    def write(self, data: bytes) -> None:
        self._dev.write(self._ep_out, data)

    def read(self, size: int, timeout: float) -> bytes:
        try:
            return bytes(self._dev.read(self._ep_in, max(size, 64), int(timeout * 1000)))
        except Exception as exc:
            if "timeout" in str(exc).lower():
                return b""
            raise TransportError(str(exc)) from exc

    def close(self) -> None:
        usb_util = _import("usb.util")
        usb_util.dispose_resources(self._dev)


class HidTransport:
    """hidapi reports. ``device`` may be any object with hidapi's write/read/close."""

    message_oriented = True

    def __init__(self, vid: int = 0, pid: int = 0, *, path: bytes | None = None, device: Any = None) -> None:
        if device is None:
            hid = _import("hid")
            device = hid.device()
            if path is not None:
                device.open_path(path)
            else:
                device.open(vid, pid)
        self._dev = device

    def write(self, data: bytes) -> None:
        self._dev.write(list(data))

    def read(self, size: int, timeout: float) -> bytes:
        return bytes(self._dev.read(max(size, 64), int(timeout * 1000)))

    def close(self) -> None:
        self._dev.close()


class LoopbackTransport:
    """In-process transport to a simulator object with ``handle`` and ``poll`` methods."""

    def __init__(self, simulator: Any, *, message_oriented: bool = False) -> None:
        self.message_oriented = message_oriented
        self._sim = simulator
        self._pending: queue.Queue[bytes] = queue.Queue()
        self.on_write: Callable[[bytes], None] | None = None

    def write(self, data: bytes) -> None:
        if self.on_write is not None:
            self.on_write(data)
        out = self._sim.handle(data)
        if out:
            if self.message_oriented:
                for message in out if isinstance(out, list) else [out]:
                    self._pending.put(bytes(message))
            else:
                self._pending.put(bytes(out))

    def read(self, size: int, timeout: float) -> bytes:
        try:
            return self._pending.get_nowait()
        except queue.Empty:
            pass
        poll = getattr(self._sim, "poll", None)
        if poll is not None:
            data = poll()
            if data:
                return bytes(data)
        try:
            return self._pending.get(timeout=timeout)
        except queue.Empty:
            return b""

    def close(self) -> None:
        close = getattr(self._sim, "close", None)
        if close is not None:
            close()


def _parse_host_port(resource: str) -> tuple[str, int]:
    text = resource.removeprefix("tcp://").removeprefix("socket://")
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise TransportError(f"expected host:port, got {resource!r}")
    return host, int(port)


def _parse_vid_pid(resource: str) -> tuple[int, int]:
    vid, _, pid = resource.partition(":")
    return int(vid, 16), int(pid, 16)


def open_transport(transport: dict[str, Any], resource: str) -> Transport:
    """Open the transport described by a protocol's ``transport`` section.

    ``tcp://host:port`` works for every transport kind so a driver can talk to
    a simulator or a network bridge; message-oriented kinds use message mode.
    """
    kind = str(transport.get("kind", "serial"))
    settings: dict[str, Any] = dict(transport.get("settings") or {})
    message_kind = kind in ("hid", "winusb")
    if resource.startswith("tcp://"):
        host, port = _parse_host_port(resource)
        return SocketTransport(host, port, message_mode=message_kind)
    if kind == "serial":
        return SerialTransport(resource, settings)
    if kind == "tcp":
        host, port = _parse_host_port(resource)
        return SocketTransport(host, port)
    if kind == "visa":
        return VisaTransport(resource, settings)
    if kind == "ftdi":
        return FtdiTransport(resource, settings)
    if kind == "winusb":
        vid, pid = _parse_vid_pid(resource)
        return UsbTransport(vid, pid, settings)
    if kind == "hid":
        if resource.startswith("path:"):
            return HidTransport(path=resource.removeprefix("path:").encode())
        vid, pid = _parse_vid_pid(resource)
        return HidTransport(vid, pid)
    raise TransportError(f"unknown transport kind {kind!r}")
