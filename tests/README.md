# VBC Test Suite

Current test documentation for VBC.

## Scope

- `tests/unit/` contains unit tests (domain logic, adapters, UI, config, CLI).
- `tests/integration/` contains integration tests for pipeline flow.
- `tests/test_docs_sync.py` checks docs/code sync (CLI/config/events).

## Quick Start

```bash
# All tests
uv run pytest

# Unit tests only
uv run pytest tests/unit/

# Integration tests only
uv run pytest tests/integration/

# Skip slow tests
uv run pytest -m "not slow"

# Coverage
uv run pytest --cov=vbc --cov-report=html

# Docs sync test
uv run pytest tests/test_docs_sync.py -q
```

## Specific Cases

```bash
# Single file
uv run pytest tests/unit/test_config_models.py

# Single test
uv run pytest tests/unit/test_config_models.py::test_general_config_defaults
```

## Current Test Count

Do not keep static counters in this file. Check the current count with:

```bash
uv run pytest --collect-only -q
```

## Environment Notes

- Some integration tests require system tools (`ffmpeg`, `exiftool`).
- Tests rely on `tmp_path` and mocks; no fixed test media is required in the repo.
