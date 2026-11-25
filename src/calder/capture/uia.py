"""UI Automation recording and driving through the ``calder-uia`` helper.

The helper (native/uirecorder, C# on System.Windows.Automation) subscribes to
invoke, value, selection, toggle and text events of a process's windows and
prints one JSON object per line. Timestamps are QueryPerformanceCounter ticks,
the same clock the capture DLL uses, so wire and UI events align exactly.
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from calder.capture.launcher import LaunchError, find_native
from calder.session.models import UiElement, UiKind

KINDS = {k.value: k for k in UiKind}


@dataclass
class RawUiEvent:
    qpc: int
    kind: UiKind
    element: UiElement
    value: str | None


def parse_line(line: str) -> RawUiEvent | dict[str, Any] | None:
    """A UI event, a control message (``{"type": ...}``), or None for noise."""
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if data.get("type") != "event":
        return dict(data)
    kind = KINDS.get(str(data.get("kind")))
    if kind is None:
        return None
    el = data.get("element") or {}
    element = UiElement(
        name=str(el.get("name", "")),
        automation_id=str(el.get("automation_id", "")),
        control_type=str(el.get("control_type", "")),
        label=str(el.get("label", "")),
        window=str(el.get("window", "")),
    )
    value = data.get("value")
    return RawUiEvent(int(data.get("qpc", 0)), kind, element, None if value is None else str(value))


def helper_path() -> Path:
    return find_native("calder-uia.exe")


@dataclass
class UiaRecorder:
    pid: int
    events: list[RawUiEvent] = field(default_factory=list)
    ready: threading.Event = field(default_factory=threading.Event)
    errors: list[str] = field(default_factory=list)
    _proc: subprocess.Popen[bytes] | None = None
    _thread: threading.Thread | None = None

    def start(self, timeout: float = 30.0) -> UiaRecorder:
        try:
            exe = helper_path()
        except LaunchError as exc:
            raise LaunchError(f"UI recording unavailable: {exc}") from exc
        self._proc = subprocess.Popen(
            [str(exe), "record", "--pid", str(self.pid)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._thread = threading.Thread(target=self._pump, daemon=True, name="calder-uia")
        self._thread.start()
        if not self.ready.wait(timeout):
            self.errors.append("UI recorder did not attach to the application window in time")
        return self

    def _pump(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            parsed = parse_line(raw.decode("utf-8", "replace"))
            if isinstance(parsed, RawUiEvent):
                self.events.append(parsed)
            elif isinstance(parsed, dict):
                if parsed.get("type") == "ready":
                    self.ready.set()
                elif parsed.get("type") == "error":
                    self.errors.append(str(parsed.get("message")))

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                assert self._proc.stdin is None
                self._proc.terminate()
                self._proc.wait(5)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=5)


def drive(pid: int, script: str | Path, *, timeout: float = 120.0) -> None:
    """Replay a UI script (set values, select items, click buttons) against a process."""
    exe = helper_path()
    result = subprocess.run(
        [str(exe), "drive", "--pid", str(pid), "--script", str(script)],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise LaunchError(
            "UI drive failed: " + result.stderr.decode("utf-8", "replace") + result.stdout.decode("utf-8", "replace")
        )
