# Contributing to Calder

Thank you for helping free lab instruments. Contributions of code, documentation, simulators
and (shareable) example sessions are welcome.

## Development setup

Requirements: Python 3.12+ with [uv](https://docs.astral.sh/uv/). For native work on Windows:
Visual Studio Build Tools ("Desktop development with C++", x86 and x64), CMake, vcpkg
(`VCPKG_ROOT`), and the .NET 10 SDK.

```
uv sync --all-extras --dev
uv run pytest -m "not capture"          # pure-Python suite, any OS
python scripts/build_native.py          # Windows: DLLs, launchers, UI helper, vendor app
uv run pytest tests/capture             # Windows: capture tests
```

## Before you open a pull request

```
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy
uv run pytest -m "not capture"
```

## Rules of the road

- **Evidence everywhere.** Every inferred element records the method, the supporting events,
  a confidence and the alternatives considered. A new heuristic needs a test and an entry in
  `docs/inference.md`; there are no unexplained magic numbers.
- **Safety first.** Changes that affect what `verify`, generated drivers or generated servers
  may send must keep every rule in `docs/safety.md` and come with tests in `tests/test_safety.py`.
- **The runtime stays standard-library only.** `src/calder/runtime/` is copied into generated
  packages; it may only use relative imports and the standard library.
- **Native hooks stay minimal.** Hooks call the real function first, preserve the last error,
  never block the vendor application, and are documented in `docs/hooks.md`.
- **Typing.** Python is `mypy --strict` clean; external formats are pydantic models; no bare
  `except`.
- **Comments explain why, not what.**
- **Never commit secrets** or real captured sessions; only small synthetic fixtures.

## Simulators and ground truth

A new protocol pattern is easiest to support with a simulator in `tests/simulators/`, a
session generator in `tests/simulators/sessions.py` and ground-truth assertions in
`tests/test_ground_truth.py`.

## Legal

Contribute only material you have the right to contribute. Do not submit recordings or
protocol details obtained in violation of a license or agreement.

By contributing you agree that your contributions are licensed under the Apache License 2.0.
Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).
