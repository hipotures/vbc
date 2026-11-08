# AGENTS.md

This file provides guidance to coding agents working with this repository.

## Repository Overview

VBC (Video Batch Compression) is a production-grade video batch compression tool with a real-time UI and an event-driven Clean Architecture.

Project uses `uv` for dependency management (Python 3.12+). All commands use `uv run`.

**Migration in progress: service split.** The source of truth is
`docs/architecture/service-split.md`. Read it before changing code. Summary:

- `vbc_legacy/` is today's complete application, frozen. `vbc run <dirs>`
  runs it. No new features there; a critical bug fix goes into both trees.
- `vbc/` is the new tree (server `vbc serve`, clients `vbc tui` and one-shot
  API commands). It takes over from `vbc_legacy/` only what it needs, by
  copying whole files or cutting out fragments.
- The two packages never import each other. The only bridge is the `run`
  command in `vbc/cli.py`.
- `vbc_legacy/` is deleted at the end of the migration.
- If implementation shows that an architectural decision in the plan is
  wrong, stop. Fix the plan and reimplement from the tag
  `service-split-baseline` (or from the last good step) on a fresh branch.
  Do not work around a wrong decision, and never force-push or move tags.

## Common Commands

### Run VBC
```bash
# Main application (legacy, in-process TUI)
uv run vbc run /path/to/videos --gpu --threads 8
uv run vbc run /path/to/videos --cpu --quality 35

# Rate mode example
uv run vbc run /path/to/videos --quality-mode rate --bps 200Mbps

# Test with a small dataset
uv run vbc run /path/to/test/videos --threads 2 --quality 45
```

A bare `vbc <dirs>` is rejected with a hint; all commands are peer
subcommands of `vbc` (`vbc/cli.py`).

### Documentation
```bash
# Build and serve documentation
./scripts/serve-docs.sh  # Serves docs at http://127.0.0.1:8000
uv run --extra docs mkdocs build --strict
```

### Dependency Management
```bash
# Add new dependency
uv add <package>

# Add dev dependency (optional extra)
uv add --optional dev <package>

# Add docs dependency (optional extra)
uv add --optional docs <package>

# Sync dependencies
uv sync --extra dev --extra docs
```

## VBC Architecture

VBC follows Clean Architecture with strict layer separation:

```
UI Layer (ui/)               -> Rich dashboard, keyboard listener
    Events (EventBus)
Pipeline Layer (pipeline/)   -> Orchestrator (job lifecycle)
    Domain Models
Infrastructure (infrastructure/) -> FFmpeg, ExifTool, FFprobe adapters
```

The target architecture (server read model, command service, HTTP API,
clients) is described in `docs/architecture/service-split.md`.

### Directory Structure
```
vbc/                     # new tree (being built)
└── cli.py               # Typer app: peer subcommands (`run` for now)
vbc_legacy/              # frozen, today's application
├── main.py              # legacy Typer command (`vbc run`)
├── config/              # Pydantic models + YAML loader
├── domain/              # Business logic (models.py, events.py)
├── infrastructure/      # External adapters (event_bus, ffmpeg, exif_tool, ffprobe, logging)
├── pipeline/            # Orchestrator (core processing logic)
└── ui/                  # Rich Live dashboard (state, manager, keyboard, dashboard)
```

### Key Components (legacy, carried into the new tree)

**Orchestrator** (`pipeline/orchestrator.py`):
- Discovery, queue management, job lifecycle
- ThreadController pattern (Condition-based concurrency)
- Submit-on-demand pattern (deque with prefetch factor)
- Metadata caching (thread-safe ExifTool calls)
- Graceful shutdown, dynamic refresh

**EventBus** (`infrastructure/event_bus.py`):
- Synchronous Pub/Sub for domain events
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

Critical: Preserve event-driven architecture. Do not create direct dependencies between layers.
Important: Never add `conf/vbc.yaml` to git tracking. If it appears tracked, notify the user immediately.

## Dependencies

Package manager: `uv` (defined in `pyproject.toml`)

VBC dependencies:
- `pydantic` - Data models and validation
- `rich` - Dashboard UI
- `pyyaml` - Config loading
- `pyexiftool` - Metadata extraction/copying
- `typer` - CLI framework
- System: `ffmpeg`, `exiftool` binaries

Optional dependency extras:
- `dev`: `pytest` (runner for the e2e suite)
- `docs`: `mkdocs`, `mkdocs-material`, `mkdocstrings`, `pymdown-extensions`

Document any new dependencies in `README.md` and relevant `docs/` pages.

## Documentation

VBC has MkDocs documentation in `docs/`:
- `getting-started/` - Installation, quickstart, configuration
- `user-guide/` - CLI, runtime controls, advanced features
- `architecture/` - Overview, events, pipeline flow, service split plan
- `api/` - Auto-generated from code (points at `vbc_legacy` until modules move to `vbc`)
- `development/` - Contributing and testing workflow

Build docs: `./scripts/serve-docs.sh`

For major VBC changes, update relevant docs in `docs/` alongside code.

## Testing

**No unit tests.** The old unit/integration suite was removed. New code is
covered only by a small black-box end-to-end suite in `tests/e2e/` (created
from step 5 of the plan). The suite starts real processes with real
ffmpeg/exiftool, uses media fixtures generated at test time (nothing binary in
git) and checks only externally visible results. See "Testing strategy" in
`docs/architecture/service-split.md`.

```bash
uv run pytest            # runs tests/e2e (once it exists)
```

Keep the suite fast (minutes). A scenario that keeps breaking for reasons
other than real regressions gets simplified or deleted.
