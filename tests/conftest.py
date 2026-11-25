"""Shared fixtures: synthetic sessions and inferred models per simulator (cached per run)."""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

import pytest

from calder.infer.pipeline import InferOptions, infer
from calder.model.schema import ProtocolModel
from calder.session.models import Session
from tests.simulators.sessions import sessions as make_sessions

ROOT = Path(__file__).resolve().parents[1]
KINDS = ("thermostat", "pump", "modbus", "balance", "hid")


@functools.cache
def sessions_for(kind: str, count: int = 10) -> tuple[Session, ...]:
    return tuple(make_sessions(kind, count))


@functools.cache
def model_for(kind: str) -> ProtocolModel:
    return infer(list(sessions_for(kind)), InferOptions(name=kind))


@pytest.fixture(params=KINDS)
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def native_dir(arch: str) -> Path:
    env = os.environ.get("CALDER_NATIVE_DIR")
    base = Path(env).parent if env else ROOT / "out" / "native"
    return base / arch


def vendorapp(arch: str) -> Path:
    return ROOT / "out" / "vendorapp" / arch / "CalderVendorApp.exe"


def capture_ready(arch: str) -> str | None:
    """Reason the capture tests cannot run for ``arch``, or None."""
    if sys.platform != "win32":
        return "capture needs Windows"
    bits = "64" if arch == "x64" else "32"
    needed = [native_dir(arch) / f"calder_capture{bits}.dll", native_dir(arch) / f"calder_launch{bits}.exe"]
    missing = [str(p) for p in needed if not p.is_file()]
    if missing:
        return "native binaries not built (python scripts/build_native.py): " + ", ".join(missing)
    return None
