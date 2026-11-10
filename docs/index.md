# VBC - Video Batch Compression

**Modular, high-performance video batch compression tool with real-time UI and advanced features.**

## Overview

VBC is a production-ready Python application for batch video compression using modern codecs (AV1) with GPU (NVENC) and CPU (SVT-AV1) support. Built with clean architecture principles, it offers:

- 🚀 **High Performance**: Multi-threaded processing with dynamic concurrency control
- 🎯 **Smart Compression**: Camera-specific quality settings and auto-rotation
- 🎨 **Rich UI & Headless Server**: Real-time interactive TUI (`vbc tui`), HTTP JSON API (`vbc serve`), and one-shot CLI commands (`vbc status`, `vbc dirs`, `vbc config`, etc.)
- 🔧 **Flexible**: Extensive configuration via YAML + CLI overrides + remote API updates
- 🏗️ **Clean Architecture**: Event-driven design with strict server/client separation

## Key Features

### Processing
- **Multi-codec support**: AV1 (NVENC/SVT-AV1), with fallback and hardware detection
- **Dynamic Quality (CQ)**: Per-camera model quality settings via configuration
- **Auto-rotation**: Regex-based filename pattern matching for automatic rotation
- **Smart filtering**: Skip AV1-encoded files, camera model filtering, size thresholds
- **Color space fixes**: Automatic handling of FFmpeg 7.x "reserved" color space issues
- **Deep metadata**: Full EXIF/XMP preservation with custom VBC tags

### Runtime Control
- **Interactive UI**: 6-panel Rich dashboard with live statistics
- **Keyboard controls**: Adjust threads (`<`/`>`), graceful shutdown (`S`), refresh queue (`R`)
- **Submit-on-demand**: Intelligent queue management with prefetch control
- **Graceful shutdown**: Clean termination of active compressions
- **Progress tracking**: Real-time throughput, ETA, and per-file progress

### Reliability
- **Error handling**: Hardware capability detection, corrupted file skipping
- **Resume capability**: Automatic skip of already-compressed files
- **Error markers**: `.err` files for failed compressions with cleanup options
- **Min ratio check**: Keep original if compression savings below threshold

## Architecture

VBC follows **Clean Architecture** with clear separation between the headless server and presentation clients:

```
┌─────────────────────────────────────────────────┐
│              Clients (vbc/clients/)             │
│     vbc tui (Rich)   │   vbc status/dirs/…      │
└──────────────────────┬──────────────────────────┘
                       │ HTTP JSON API (/api/v1)
┌──────────────────────┴──────────────────────────┐
│            Service Layer (vbc/service/)         │
│       StateStore, CommandService, FastAPI       │
└──────────────────────┬──────────────────────────┘
                       │ Events (EventBus)
┌──────────────────────┴──────────────────────────┐
│           Pipeline Layer (vbc/pipeline/)        │
│                 (Orchestrator)                  │
└──────────────────────┬──────────────────────────┘
                       │ Domain Models
┌──────────────────────┴──────────────────────────┐
│      Infrastructure Layer (vbc/infrastructure/) │
│  (FFmpeg, ExifTool, FFprobe, Watchers, Monitors)│
└─────────────────────────────────────────────────┘
```

All server components communicate via in-process **EventBus** for loose coupling, while clients interact exclusively through the versioned HTTP API.

## Quick Example

```bash
# Headless server + TUI / CLI clients
uv run vbc serve
uv run vbc dirs add /path/to/videos
uv run vbc threads 8
uv run vbc tui
uv run vbc status

# One terminal: private server + dashboard (q quits both)
uv run vbc run /path/to/videos

# With a different config file (all settings live in config / VBC.YAML)
uv run vbc run /path/to/videos --config conf/archive.yaml
```

## Runtime Controls

While VBC is running (`vbc tui` or `vbc run`), use these keyboard shortcuts:

| Key | Action |
|-----|--------|
| `<` or `,` | Decrease active threads |
| `>` or `.` | Increase active threads |
| `S` | Graceful stop of processing (finish active jobs; press `S` again while stopping to cancel, or `R` once stopped to resume) |
| `R` | Refresh file queue (scan for new files; resumes stopped/paused server) |
| `Q` / `Ctrl+Q` / `Ctrl+C` | Quit (`vbc tui` leaves its server running; `vbc run` stops its private server) |
| `Shift+U` | Unlock keyboard after 60s idle auto-lock |
| `P` | Open Prefs tab |
| `F` | Open I/O tab |
| `D` | Open Dirs tab |
| `L` | Open Logs tab (session errors) |
| `E` | Open Ref tab |
| `K` or `M` | Open the menu (Keys tab) |
| `T` | Open TUI tab |
| `I` | Cycle overlay dim level |
| `G` | Rotate GPU metric |
| `W` / `O` | Cycle sparkline preset / palette |
| `Tab` | Cycle overlay tabs |
| `[` / `]` | Logs tab page prev/next |

## Next Steps

- [Installation](getting-started/installation.md) - Set up VBC
- [Quick Start](getting-started/quickstart.md) - First compression job
- [Server & Clients](user-guide/server.md) - Run `vbc serve`, `vbc tui`, and CLI commands
- [Configuration](getting-started/configuration.md) - Customize settings
- [Architecture](architecture/overview.md) - Understand the design
- [API Reference](api/config.md) - Explore the codebase
