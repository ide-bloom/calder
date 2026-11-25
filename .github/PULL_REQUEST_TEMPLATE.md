## What and why

## How it was tested

- [ ] `uv run ruff check src tests scripts` and `uv run ruff format --check src tests scripts`
- [ ] `uv run mypy`
- [ ] `uv run pytest -m "not capture"`
- [ ] Capture tests (`uv run pytest tests/capture`) if native code, capture or the vendor app changed

## Checklist

- [ ] New inference heuristics record evidence, have a test, and are described in `docs/inference.md`
- [ ] Documentation matches the new behavior
- [ ] `CHANGELOG.md` updated under "Unreleased"
- [ ] Changes that affect what `verify` may send were reviewed against `docs/safety.md`
