# UI API (`vbc tui`)

This page documents the Rich-based interactive TUI client (`vbc/clients/tui/`).

## Client State

Thread-safe state container (`ClientState`) combining the server read-model snapshot with local presentation state and anchor-based counter extrapolation.

::: vbc.clients.tui.state
    options:
      show_source: true
      heading_level: 3

## TUI Controller

Keyboard event subscriber (`TuiController`) that dispatches API commands (`PUT /threads`, `POST /refresh`, `POST/DELETE /stop`, `PUT /dirs`, `POST /dirs/scan`) and manages overlay tabs.

::: vbc.clients.tui.controller
    options:
      show_source: true
      heading_level: 3

## Server Poller

Background long-poll worker (`Poller`) for `/api/v1/state`, `/api/v1/gpu`, and on-demand overlay endpoints.

::: vbc.clients.tui.poller
    options:
      show_source: true
      heading_level: 3

## Keyboard Listener

Non-blocking keyboard input handler with 60s idle auto-lock (`Shift+U` unlock) and bracketed-paste protection.

::: vbc.clients.tui.keyboard
    options:
      show_source: true
      heading_level: 3

## Dashboard

Rich Live dashboard with status, progress, activity, queue, active job, and overlay surfaces.

::: vbc.clients.tui.dashboard
    options:
      show_source: true
      heading_level: 3

## Modern Overlays

Tabbed overlay renderers for Settings, Shortcuts, I/O, Dirs, TUI, Reference, and Logs content.

::: vbc.clients.tui.modern_overlays
    options:
      show_source: true
      heading_level: 3

## GPU Sparklines

Sparkline presets, palettes, and rendering helpers for GPU/CPU metrics.

::: vbc.clients.tui.gpu_sparkline
    options:
      show_source: true
      heading_level: 3

## Usage Examples

### Starting the TUI Client

`vbc tui` calls `run_tui` (`vbc/clients/tui/app.py`), which wires `ApiClient`,
`ClientState`, the client-local `EventBus` (`vbc/clients/tui/bus.py`), `Poller`,
`TuiController`, `KeyboardListener` and `Dashboard`, and returns the exit code:

```python
from vbc.clients.tui.app import run_tui

# api: server URL (default: $VBC_API, then http://127.0.0.1:8770)
exit_code = run_tui(api="http://127.0.0.1:8770", log_file=None)
```

The client never imports server modules (`vbc.service`, `vbc.infrastructure`);
it talks to the server only through the HTTP API.

## Dashboard Surfaces

The dashboard is rendered as a compact Rich layout rather than a fixed list of legacy panels:

- Top status/KPI area with runtime counters, hints, and optional GPU sparkline metrics.
- Size-based progress area with total/session counters and ETA.
- Active jobs area with per-file progress.
- Activity and completed-job history.
- Queue preview for pending files.
- Tabbed overlay system for Settings, Shortcuts, I/O, Dirs, TUI, Reference, and Logs.

## Dashboard Rendering

```python
from vbc.ui.dashboard import Dashboard
from vbc.ui.state import UIState

state = UIState()

# Set configuration lines (shown in config overlay)
state.config_lines = [
    "Video Batch Compression - NVENC AV1 (GPU)",
    "Start: 2025-12-21 15:30:00",
    "Input: /videos",
    "Output: /videos_out",
    "Threads: 8 (Prefetch: 1x)",
    "Encoder: NVENC AV1 | Preset: p7 (Slow/HQ)",
    "Quality: CQ45 (Global Default)",
    "Dynamic Quality: ILCE-7RM5:38, DC-GH7:40",
]

# Create dashboard
dashboard = Dashboard(state)

# Render in context
with dashboard:
    # Your processing loop
    # Dashboard updates automatically every 1s
    pass
```

## Thread Safety

All UI state updates use locks:

```python
class UIState:
    def __init__(self):
        self._lock = threading.RLock()

    def add_completed_job(self, job, output_size):
        with self._lock:
            self.completed_count += 1
            self.total_input_bytes += job.source_file.size_bytes
            self.total_output_bytes += output_size
            # ...
```

## Unicode Handling

```python
from vbc.ui.state import UIState

state = UIState()
state.strip_unicode_display = True  # Default

# Sanitize filename for display
def sanitize(filename: str) -> str:
    if not state.strip_unicode_display:
        return filename
    return ''.join(c if ord(c) < 128 else '?' for c in filename)

# Example
filename = "video_🎬_final.mp4"
display = sanitize(filename)
# Output: "video_?_final.mp4" (prevents table alignment issues)
```

## Keyboard Event Reference

| Key | Event | Handler (`TuiController`) |
|-----|-------|---------------------------|
| `<` or `,` | `ThreadControlEvent(change=-1)` | Sends `PUT /api/v1/threads` (`current - 1`) |
| `>` or `.` | `ThreadControlEvent(change=+1)` | Sends `PUT /api/v1/threads` (`current + 1`) |
| `S` or `s` | `RequestShutdown()` | Sends `POST /api/v1/stop` (or `DELETE /api/v1/stop` if already stopping) |
| `R` or `r` | `RefreshRequested()` | Sends `POST /api/v1/refresh` |
| `P` or `p` | `ToggleOverlayTab(tab="settings")` | Toggles Prefs tab |
| `F` or `f` | `ToggleOverlayTab(tab="io")` | Toggles I/O tab |
| `K` or `M` | `ToggleOverlayTab(tab="shortcuts")` | Toggles Keys tab |
| `D` or `d` | `ToggleOverlayTab(tab="dirs")` | Toggles Dirs tab |
| `T` or `t` | `ToggleOverlayTab(tab="tui")` | Toggles TUI tab |
| `E` or `e` | `ToggleOverlayTab(tab="reference")` | Toggles Ref tab |
| `L` or `l` | `ToggleOverlayTab(tab="logs")` | Toggles Logs tab |
| `I` or `i` | `CycleOverlayDim(direction=+1)` | Cycles overlay dim level |
| `G` or `g` | `RotateGpuMetric()` | Rotates the GPU metric shown in the top bar |
| `W` or `w` | `CycleSparklinePreset(direction=+1)` | Cycles sparkline presets |
| `O` or `o` | `CycleSparklinePalette(direction=+1)` | Cycles sparkline palettes |
| `[` / `]` | `CycleLogsPage(direction=-1/+1)` | Pages Logs tab |
| `Tab` | `CycleOverlayTab(direction=+1)` | Cycles tabs |
| `Esc` | `CloseOverlay()` | Closes active overlay |
| `Q` / `Ctrl+Q` / `Ctrl+C` | `InterruptRequested()` | Quits `vbc tui` client |
| `Shift+U` | *(handled in `KeyboardListener`)* | Unlocks keyboard after idle auto-lock |

## Prefs Overlay

Toggle with `P` key:

```
┌─ CONFIGURATION ────────────────────────────────┐
│ Video Batch Compression - NVENC AV1 (GPU)     │
│ Start: 2025-12-21 15:30:00                    │
│ Input: /videos                                │
│ Output: /videos_out                           │
│ Threads: 8 (Prefetch: 1x)                    │
│ Encoder: NVENC AV1 | Preset: p7 (Slow/HQ)    │
│ Audio: Auto (lossless->AAC 256k, AAC/MP3 copy, other->AAC 192k) │
│ Quality: CQ45 (Global Default)                │
│ Dynamic Quality: ILCE-7RM5:38, DC-GH7:40           │
│ Camera Filter: None                           │
│ Metadata: Deep (ExifTool + XMP)              │
│ Autorotate: 3 rules loaded                   │
│ Manual Rotation: None                         │
│ Extensions: .mp4, .mov, .avi → .mp4          │
│ Min size: 1.0MB | Skip AV1: false            │
│ Clean errors: false | Strip Unicode: true    │
│ Debug logging: false                          │
│                                               │
│ Press Esc to close                            │
└────────────────────────────────────────────────┘
```
