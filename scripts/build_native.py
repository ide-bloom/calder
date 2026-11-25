"""Build Calder's native components on Windows.

* native/  -> calder_capture32/64.dll, calder_launch32/64.exe and the test tools
             (CMake + vcpkg manifest mode for Microsoft Detours), into out/native/<arch>/
* native/uirecorder -> calder-uia.exe (UI Automation helper), into out/native/x64/
* tests/vendorapp   -> the WPF test vendor application (x86 and x64), into out/vendorapp/<arch>/

Usage: python scripts/build_native.py [--arch x64 x86] [--config Release] [--skip-dotnet]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
PLATFORMS = {"x64": ("x64", "x64-windows-static"), "x86": ("Win32", "x86-windows-static")}


def run(cmd: list[str], **kwargs: object) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kwargs)  # type: ignore[call-overload]


def vcpkg_root() -> Path:
    for candidate in (os.environ.get("VCPKG_ROOT"), os.environ.get("VCPKG_INSTALLATION_ROOT"), r"C:\vcpkg"):
        if candidate and (Path(candidate) / "scripts" / "buildsystems" / "vcpkg.cmake").is_file():
            return Path(candidate)
    raise SystemExit("vcpkg not found: set VCPKG_ROOT")


def build_cpp(arch: str, config: str) -> Path:
    platform, triplet = PLATFORMS[arch]
    build = OUT / f"build-native-{arch}"
    dest = OUT / "native" / arch
    toolchain = vcpkg_root() / "scripts" / "buildsystems" / "vcpkg.cmake"
    run(
        [
            "cmake",
            "-S",
            str(ROOT / "native"),
            "-B",
            str(build),
            "-A",
            platform,
            f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
            f"-DVCPKG_TARGET_TRIPLET={triplet}",
            f"-DVCPKG_INSTALLED_DIR={OUT / 'vcpkg_installed'}",
            f"-DCALDER_OUTPUT_DIR={dest}",
        ]
    )
    run(["cmake", "--build", str(build), "--config", config, "--parallel"])
    return dest


def build_dotnet(config: str) -> None:
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise SystemExit("dotnet SDK not found")
    run(
        [
            dotnet,
            "build",
            str(ROOT / "native" / "uirecorder" / "CalderUia.csproj"),
            "-c",
            config,
            "-r",
            "win-x64",
            "--no-self-contained",
            "-o",
            str(OUT / "native" / "x64"),
        ]
    )
    for arch in ("x64", "x86"):
        run(
            [
                dotnet,
                "build",
                str(ROOT / "tests" / "vendorapp" / "CalderVendorApp.csproj"),
                "-c",
                config,
                "-r",
                f"win-{arch}",
                "--no-self-contained",
                "-o",
                str(OUT / "vendorapp" / arch),
            ]
        )


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("native components build on Windows only")
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", nargs="+", default=["x64", "x86"], choices=sorted(PLATFORMS))
    parser.add_argument("--config", default="Release")
    parser.add_argument("--skip-dotnet", action="store_true")
    args = parser.parse_args()
    for arch in args.arch:
        print(f"native {arch} -> {build_cpp(arch, args.config)}")
    if not args.skip_dotnet:
        build_dotnet(args.config)


if __name__ == "__main__":
    main()
