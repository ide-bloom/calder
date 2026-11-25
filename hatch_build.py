"""Hatch build hook: a wheel that carries the native capture binaries is Windows-only.

The release workflow copies the DLLs, launchers and UI helper into
src/calder/native before building; that wheel gets a win_amd64 tag (it contains
both the 32-bit and 64-bit capture DLLs, for 32- and 64-bit vendor applications).
Without the binaries the wheel stays pure Python.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):  # type: ignore[misc]
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        del version
        if self.target_name != "wheel":
            return
        native = Path(self.root) / "src" / "calder" / "native"
        if any(native.glob("*.dll")):
            build_data["pure_python"] = False
            build_data["tag"] = "py3-none-win_amd64"
            build_data["artifacts"].append("src/calder/native/*")
