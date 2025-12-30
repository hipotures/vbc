# VBC - Video Batch Compression

VBC is a modular, high-performance tool for batch video compression with a real-time UI and clean architecture.

## Overview

- AV1 compression with GPU (NVENC) or CPU (SVT-AV1)
- Dynamic quality per camera model, auto-rotation, and smart filtering
- Rich TUI dashboard with live progress and runtime controls
- YAML configuration with CLI overrides
- Metadata preservation via ExifTool

## Requirements

- Python 3.12+
- FFmpeg 6.0+ with AV1 support (av1_nvenc and/or libsvtav1)
- ExifTool (optional but recommended)
- Linux, macOS, or Windows (WSL)

## Installation

VBC uses `uv` for dependency management.

```bash
# Install dependencies
uv sync

# Verify
uv run vbc/main.py --help
```

Full installation details are in `docs/getting-started/installation.md`.

## Quick Start

```bash
# Basic run
uv run vbc/main.py /path/to/videos

# GPU acceleration with more threads
uv run vbc/main.py /path/to/videos --gpu --threads 8

# CPU mode for higher quality
uv run vbc/main.py /path/to/videos --cpu --cq 35
```

Output is written to `{INPUT_DIR}_out/` with a `compression.log` file and optional `.err` markers.

## Configuration

Default configuration is `conf/vbc.yaml`. CLI arguments override config values.

```bash
uv run vbc/main.py /path/to/videos --config conf/vbc.yaml --threads 8 --cq 40
```

See `docs/getting-started/configuration.md` for the full reference.

## Runtime Controls

While running, use these keyboard shortcuts:

| Key | Action |
| --- | --- |
| `<` or `,` | Decrease threads |
| `>` or `.` | Increase threads |
| `S` | Graceful shutdown (press again to cancel) |
| `R` | Refresh queue |
| `C` | Toggle configuration overlay |
| `Ctrl+C` | Immediate interrupt |

Full reference: `docs/user-guide/runtime-controls.md`.

## Docs

```bash
./serve-docs.sh  # http://127.0.0.1:8000
```

Additional docs live under `docs/`.

## Testing

```bash
uv run pytest
uv run pytest tests/unit/
uv run pytest -m "not slow"
```

## Architecture

VBC follows Clean Architecture with event-driven communication via EventBus.

```
UI (vbc/ui) -> EventBus -> Pipeline (vbc/pipeline) -> Domain (vbc/domain)
                          -> Infrastructure (vbc/infrastructure)
```

Architecture details: `docs/architecture/overview.md`.
