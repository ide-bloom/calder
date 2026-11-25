# Third-party notices

Calder is licensed under the Apache License 2.0 (see `LICENSE`). It builds on the
following third-party software. Each is used under its own license; the full license
texts ship with the respective packages.

## Bundled in binary distributions

| Component | Use | License |
|---|---|---|
| [Microsoft Detours](https://github.com/microsoft/Detours) | Statically linked into `calder_capture32.dll`, `calder_capture64.dll`, `calder_launch32.exe`, `calder_launch64.exe` | MIT |
| Microsoft C/C++ runtime (static) | Linked into the native binaries | Microsoft Visual Studio redistribution terms |

## Python dependencies (installed from PyPI, not vendored)

| Package | License |
|---|---|
| typer | MIT |
| pydantic | MIT |
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| jinja2 | BSD-3-Clause |
| numpy | BSD-3-Clause |
| pyserial | BSD-3-Clause |
| pyvisa | MIT |
| pyusb | BSD-3-Clause |
| hidapi (Python binding) | BSD / GPL-3.0 / HIDAPI license (choice) |
| crcmod | MIT |
| sila2 | MIT |
| mcp (Model Context Protocol Python SDK) | MIT |
| httpx | BSD-3-Clause |
| rich | MIT |
| pdfplumber (optional, `calder[manual]`) | MIT |

## .NET components

`calder-uia.exe` and the WPF test vendor application use only the .NET 10 runtime and
Windows Desktop runtime (MIT), which are not redistributed.

## Reference material

The CRC catalogue in `src/calder/runtime/checksums.py` reproduces algorithm parameters
and check values from Greg Cook's *Catalogue of parametrised CRC algorithms*
(<https://reveng.sourceforge.io/crc-catalogue/>). Parameters are facts, not
copyrightable expression; the catalogue is credited here as the source.
