"""Launching vendor applications with the matching capture DLL.

The DLL is only ever injected into processes Calder launches: the native
launcher (``calder_launch32.exe`` / ``calder_launch64.exe``) starts the
vendor executable suspended through ``DetourCreateProcessWithDllExW`` with the
capture DLL of the same bitness, prints the child PID, resumes it, and exits
with the child's exit code.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import IO

IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_FILE_MACHINE_ARM64 = 0xAA64


class LaunchError(RuntimeError):
    """The vendor application could not be launched with capture."""


def pe_bitness(path: str | Path) -> int:
    """32 or 64, from the PE header's machine field (raises for non-PE files)."""
    with Path(path).open("rb") as fh:
        dos = fh.read(64)
        if len(dos) < 64 or dos[:2] != b"MZ":
            raise LaunchError(f"{path} is not a Windows executable")
        (pe_offset,) = struct.unpack_from("<I", dos, 0x3C)
        fh.seek(pe_offset)
        header = fh.read(6)
    if header[:4] != b"PE\0\0":
        raise LaunchError(f"{path} has no PE header")
    (machine,) = struct.unpack_from("<H", header, 4)
    if machine == IMAGE_FILE_MACHINE_I386:
        return 32
    if machine in (IMAGE_FILE_MACHINE_AMD64, IMAGE_FILE_MACHINE_ARM64):
        return 64
    raise LaunchError(f"unsupported machine type {machine:#06x} in {path}")


def native_dirs() -> list[Path]:
    """Where the native binaries may live: CALDER_NATIVE_DIR, the wheel, or a source build."""
    dirs: list[Path] = []
    env = os.environ.get("CALDER_NATIVE_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(Path(str(resources.files("calder"))) / "native")
    repo = Path(__file__).resolve().parents[3]
    dirs += [repo / "out" / "native" / "x64", repo / "out" / "native" / "x86", repo / "out" / "native"]
    return dirs


def find_native(name: str) -> Path:
    for d in native_dirs():
        candidate = d / name
        if candidate.is_file():
            return candidate
    raise LaunchError(
        f"{name} not found (looked in {', '.join(str(d) for d in native_dirs())}); "
        "build native/capture or set CALDER_NATIVE_DIR"
    )


@dataclass(frozen=True)
class NativeSet:
    launcher: Path
    dll: Path
    bitness: int


def natives_for(bitness: int) -> NativeSet:
    suffix = "32" if bitness == 32 else "64"
    return NativeSet(find_native(f"calder_launch{suffix}.exe"), find_native(f"calder_capture{suffix}.dll"), bitness)


@dataclass
class Launched:
    process: subprocess.Popen[bytes]
    pid: int
    bitness: int

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout)

    def terminate(self) -> None:
        if self.process.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(self.pid), "/T", "/F"], capture_output=True, check=False)
            self.process.terminate()


def launch(app: str | Path, args: list[str], env: Mapping[str, str]) -> Launched:
    """Start ``app`` under the native launcher with capture environment ``env``."""
    if sys.platform != "win32":
        raise LaunchError("capture through DLL injection needs Windows")
    exe = Path(app).resolve()
    if not exe.is_file():
        raise LaunchError(f"{exe} does not exist")
    bitness = pe_bitness(exe)
    natives = natives_for(bitness)
    full_env = {**os.environ, **env}
    cmd = [str(natives.launcher), str(natives.dll), str(exe), *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=full_env, cwd=str(exe.parent))
    assert proc.stdout is not None
    line = proc.stdout.readline().decode("ascii", "replace").strip()
    if not line.startswith("PID "):
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        proc.wait(5)
        raise LaunchError(f"launcher failed: {line} {err}".strip())
    # A console vendor app can share these pipes; keep draining them so it never blocks.
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            threading.Thread(target=_drain, args=(stream,), daemon=True, name="calder-launch-drain").start()
    return Launched(proc, int(line.split()[1]), bitness)


def _drain(stream: IO[bytes]) -> None:
    while stream.read(65536):
        pass
