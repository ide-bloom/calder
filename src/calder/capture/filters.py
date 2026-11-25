"""Capture filters: which resources and transports the DLL records.

A filter spec is a comma- or semicolon-separated list of patterns matched
case-insensitively against resource identities, with ``*`` and ``?``
wildcards::

    serial:COM5            a COM port
    serial:\\\\.\\pipe\\*        any named pipe opened with CreateFile
    tcp:192.168.1.10:5025  one instrument address
    tcp:*:502              any Modbus TCP peer
    visa:USB0::*           VISA resources
    hid:*vid_1d50*         HID devices by vendor id
    ftdi:*                 FTDI D2XX devices
    winusb:*               WinUSB devices

An empty spec records every candidate resource of the selected transports.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

TRANSPORT_NAMES = ("serial", "visa", "ftdi", "winusb", "hid", "tcp")


class FilterError(ValueError):
    pass


@dataclass(frozen=True)
class CaptureFilter:
    patterns: tuple[str, ...] = ()
    transports: tuple[str, ...] = TRANSPORT_NAMES

    @classmethod
    def parse(cls, spec: str = "", transport: str = "auto") -> CaptureFilter:
        patterns = tuple(p.strip() for p in spec.replace(";", ",").split(",") if p.strip())
        for p in patterns:
            kind = p.split(":", 1)[0].lower()
            if kind not in TRANSPORT_NAMES:
                raise FilterError(f"unknown transport in filter {p!r}; use one of {', '.join(TRANSPORT_NAMES)}")
        if transport == "auto":
            transports: tuple[str, ...] = TRANSPORT_NAMES
        else:
            transports = tuple(t.strip().lower() for t in transport.split(",") if t.strip())
            unknown = [t for t in transports if t not in TRANSPORT_NAMES]
            if unknown:
                raise FilterError(f"unknown transport(s): {', '.join(unknown)}")
        return cls(patterns, transports)

    def matches(self, identity: str) -> bool:
        kind = identity.split(":", 1)[0].lower()
        if kind not in self.transports:
            return False
        if not self.patterns:
            return True
        return any(fnmatch.fnmatchcase(identity.lower(), p.lower()) for p in self.patterns)

    def environment(self) -> dict[str, str]:
        return {"CALDER_FILTER": ";".join(self.patterns), "CALDER_TRANSPORTS": ",".join(self.transports)}

    def describe(self) -> str:
        pats = ", ".join(self.patterns) or "all resources"
        return f"{pats} on {', '.join(self.transports)}"
