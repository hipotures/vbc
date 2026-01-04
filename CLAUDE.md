# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

VBC (Video Batch Compression) is a production-grade video batch compression tool with a real-time UI and an event-driven Clean Architecture.

Project uses `uv` for dependency management (Python 3.12+). All commands use `uv run`.

## Common Commands

### Run VBC
```bash
# Main application
uv run vbc /path/to/videos --gpu --threads 8
uv run vbc /path/to/videos --cpu --cq 35

# Test with a small dataset
uv run vbc /path/to/test/videos --threads 2 --cq 45
```

### Documentation
```bash
# Build and serve documentation
./serve-docs.sh  # Opens http://127.0.0.1:8000
```

### Tests
```bash
uv run pytest
uv run pytest tests/unit/
uv run pytest -m "not slow"
```

### Dependency Management
```bash
# Add new dependency
uv add <package>

# Add dev dependency
uv add --group dev <package>

# Sync dependencies
uv sync
```

## VBC Architecture

VBC (`vbc/`) follows Clean Architecture with strict layer separation:

```
UI Layer (ui/)               -> Rich dashboard, keyboard listener
    Events (EventBus)
Pipeline Layer (pipeline/)   -> Orchestrator (job lifecycle)
    Domain Models
Infrastructure (infrastructure/) -> FFmpeg, ExifTool, FFprobe adapters
```

### Directory Structure
```
vbc/
├── main.py              # Typer CLI entry point
├── config/              # Pydantic models + YAML loader
├── domain/              # Business logic (models.py, events.py)
├── infrastructure/      # External adapters (event_bus, ffmpeg, exiftool, ffprobe)
├── pipeline/            # Orchestrator (core processing logic)
└── ui/                  # Rich Live dashboard (state, manager, keyboard, dashboard)
```

### Key Components

**Orchestrator** (`pipeline/orchestrator.py`):
- Discovery, queue management, job lifecycle
- ThreadController pattern (Condition-based concurrency)
- Submit-on-demand pattern (deque with prefetch factor)
- Metadata caching (thread-safe ExifTool calls)
- Graceful shutdown, dynamic refresh

**EventBus** (`infrastructure/event_bus.py`):
- Synchronous Pub/Sub (16 event types)
- Decouples UI from business logic
- See `domain/events.py` for event definitions

**FFmpegAdapter** (`infrastructure/ffmpeg.py`):
- Builds CLI args (GPU/CPU, rotation, filters)
- Progress monitoring via stdout parsing
- Hardware capability error detection
- Color space remuxing

### VBC Design Patterns

1. Event-driven communication: All components interact via EventBus
2. Dependency injection: Adapters injected into Orchestrator
3. ThreadController pattern: Condition variable for dynamic concurrency
4. Submit-on-demand: Do not queue 10K futures, submit as slots become available
5. Type safety: Pydantic models for all config and domain entities

### VBC Modification Guidelines

- UI changes: Modify `ui/` components (dashboard panels, keyboard shortcuts)
- New events: Add to `domain/events.py`, subscribe in `ui/manager.py`
- FFmpeg changes: Update `infrastructure/ffmpeg.py` (command builder)
- Job logic: Modify `pipeline/orchestrator.py` (discovery, processing, lifecycle)
- Config: Add fields to `config/models.py`, update YAML loader

Critical: Preserve event-driven architecture. Do not create direct dependencies between layers.
Important: Never add `conf/vbc.yaml` to git tracking. If it appears tracked, notify the user immediately.

## Dependencies

Package manager: `uv` (defined in `pyproject.toml`)

VBC dependencies:
- `rich` - Dashboard UI
- `pyyaml` - Config loading
- `pyexiftool` - Metadata extraction/copying
- `typer` - CLI framework
- System: `ffmpeg`, `exiftool` binaries

Dev dependencies:
- `pytest`, `pytest-cov`, `pytest-mock` (testing)
- `mkdocs`, `mkdocs-material`, `mkdocstrings` (docs)

Document any new dependencies in `README.md` and relevant `docs/` pages.

## Documentation

VBC has MkDocs documentation in `docs/`:
- `getting-started/` - Installation, quickstart, configuration
- `user-guide/` - CLI, runtime controls, advanced features
- `architecture/` - Overview, events, pipeline flow
- `api/` - Auto-generated from code

Build docs: `./serve-docs.sh`

For major VBC changes, update relevant docs in `docs/` alongside code.

## Testing

Currently manual testing. When adding tests:
```bash
uv run pytest                    # All tests
uv run pytest tests/unit/        # Unit tests only
uv run pytest -m "not slow"      # Skip integration tests
uv run pytest --cov=vbc          # With coverage
```

Mark slow tests with `@pytest.mark.slow` decorator.
