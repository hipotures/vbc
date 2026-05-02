# VBC - Video Batch Compression

**Modular, high-performance video batch compression tool with real-time UI and advanced features.**

## Overview

VBC is a production-ready Python application for batch video compression using modern codecs (AV1) with GPU (NVENC) and CPU (SVT-AV1) support. Built with clean architecture principles, it offers:

- 🚀 **High Performance**: Multi-threaded processing with dynamic concurrency control
- 🎯 **Smart Compression**: Camera-specific quality settings and auto-rotation
- 🎨 **Rich UI**: Real-time interactive dashboard with keyboard controls
- 🔧 **Flexible**: Extensive configuration via YAML + CLI overrides
- 🏗️ **Clean Architecture**: Event-driven design with dependency injection

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

VBC follows **Clean Architecture** with clear separation of concerns:

```
┌─────────────────────────────────────────────────┐
│                    UI Layer                      │
│  (Dashboard, KeyboardListener, UIState)         │
└────────────────┬────────────────────────────────┘
                 │ Events (EventBus)
┌────────────────┴────────────────────────────────┐
│              Pipeline Layer                      │
│           (Orchestrator)                         │
└────────────────┬────────────────────────────────┘
                 │ Domain Models
┌────────────────┴────────────────────────────────┐
│          Infrastructure Layer                    │
│  (FFmpeg, ExifTool, FFprobe, FileScanner)       │
└─────────────────────────────────────────────────┘
```

All components communicate via **EventBus** for loose coupling and testability.

## Quick Example

```bash
# Basic usage
uv run vbc /path/to/videos

# With custom configuration
uv run vbc /path/to/videos --config conf/vbc.yaml --threads 8 --quality 40

# GPU acceleration with camera filtering
uv run vbc /path/to/videos --gpu --camera "ILCE-7RM5,DJI"

# CPU mode with rotation and debug logging
uv run vbc /path/to/videos --cpu --rotate-180 --debug
```

## Runtime Controls

While VBC is running, use these keyboard shortcuts:

| Key | Action |
|-----|--------|
| `<` or `,` | Decrease active threads |
| `>` or `.` | Increase active threads |
| `S` | Graceful shutdown (finish active jobs) |
| `R` | Refresh file queue (scan for new files) |
| `C` | Open Prefs tab |
| `F` | Open I/O tab |
| `D` | Open Dirs tab |
| `L` | Open Logs tab (session errors) |
| `E` | Open Ref tab |
| `M` | Open Keys tab |
| `T` | Open TUI tab |
| `I` | Cycle overlay dim level |
| `G` | Rotate GPU metric |
| `W` / `P` | Cycle sparkline preset / palette |
| `Tab` | Cycle overlay tabs |
| `[` / `]` | Logs tab page prev/next |
| `Ctrl+C` | Immediate interrupt (terminate active jobs) |

## Next Steps

- [Installation](getting-started/installation.md) - Set up VBC
- [Quick Start](getting-started/quickstart.md) - First compression job
- [Configuration](getting-started/configuration.md) - Customize settings
- [Architecture](architecture/overview.md) - Understand the design
- [API Reference](api/config.md) - Explore the codebase
