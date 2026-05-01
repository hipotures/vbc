# UI API

This page documents the user interface components (Rich-based interactive dashboard).

## UI State

Thread-safe state container for the dashboard.

::: vbc.ui.state
    options:
      show_source: true
      heading_level: 3

## UI Manager

Event subscriber that updates UIState based on domain events.

::: vbc.ui.manager
    options:
      show_source: true
      heading_level: 3

## Keyboard Listener

Non-blocking keyboard input handler.

::: vbc.ui.keyboard
    options:
      show_source: true
      heading_level: 3

## Dashboard

Rich Live dashboard with status, progress, activity, queue, active job, and overlay surfaces.

::: vbc.ui.dashboard
    options:
      show_source: true
      heading_level: 3

## Modern Overlays

Tabbed overlay renderers for Settings, Shortcuts, I/O, Dirs, TUI, Reference, and Logs content.

::: vbc.ui.modern_overlays
    options:
      show_source: true
      heading_level: 3

## GPU Sparklines

Sparkline presets, palettes, and rendering helpers for GPU metrics.

::: vbc.ui.gpu_sparkline
    options:
      show_source: true
      heading_level: 3

## Usage Examples

### Complete UI Setup

```python
from pathlib import Path
from vbc.config.loader import load_config
from vbc.infrastructure.event_bus import EventBus
from vbc.ui.state import UIState
from vbc.ui.manager import UIManager
from vbc.ui.keyboard import KeyboardListener
from vbc.ui.dashboard import Dashboard

# Load config
config = load_config(Path("conf/vbc.yaml"))

# Create UI components
bus = EventBus()
ui_state = UIState()
ui_state.current_threads = config.general.threads
ui_state.strip_unicode_display = config.general.strip_unicode_display

# Create UI manager (subscribes to events)
ui_manager = UIManager(bus, ui_state)

# Create keyboard listener
keyboard = KeyboardListener(bus)

# Create dashboard
dashboard = Dashboard(ui_state)

# Start keyboard listener
keyboard.start()

try:
    # Run dashboard
    with dashboard:
        # Main processing loop here
        orchestrator.run(Path("/videos"))
finally:
    keyboard.stop()
```

### UIState Operations

```python
from vbc.ui.state import UIState
from vbc.domain.models import CompressionJob, VideoFile, JobStatus
from pathlib import Path

state = UIState()

# Add active job
job = CompressionJob(
    source_file=VideoFile(path=Path("video.mp4"), size_bytes=1000000)
)
state.add_active_job(job)

# Complete job
state.add_completed_job(job, output_size=450000)

# Check stats
print(f"Completed: {state.completed_count}")
print(f"Compression ratio: {state.compression_ratio:.2f}")
print(f"Space saved: {state.space_saved_bytes} bytes")

# Get last action (with 60s timeout)
action = state.get_last_action()
if action:
    print(f"Last action: {action}")
```

### Event-Driven UI Updates

```python
from vbc.infrastructure.event_bus import EventBus
from vbc.ui.state import UIState
from vbc.ui.manager import UIManager
from vbc.domain.events import JobStarted, JobCompleted

# Setup
bus = EventBus()
state = UIState()
manager = UIManager(bus, state)

# Publish events - UI updates automatically
job = CompressionJob(...)

bus.publish(JobStarted(job=job))
# UIManager.on_job_started() updates state.active_jobs

bus.publish(JobCompleted(job=job))
# UIManager.on_job_completed() updates counters and recent_jobs
```

### Keyboard Controls

```python
from vbc.infrastructure.event_bus import EventBus
from vbc.ui.keyboard import KeyboardListener, ThreadControlEvent, RequestShutdown

bus = EventBus()

# Subscribe to keyboard events
def on_thread_control(event: ThreadControlEvent):
    print(f"Thread change: {event.change:+d}")

def on_shutdown(event: RequestShutdown):
    print("Shutdown requested!")

bus.subscribe(ThreadControlEvent, on_thread_control)
bus.subscribe(RequestShutdown, on_shutdown)

# Start listener
keyboard = KeyboardListener(bus)
keyboard.start()

# User presses '>' → ThreadControlEvent(change=+1)
# User presses '<' → ThreadControlEvent(change=-1)
# User presses 'S' → RequestShutdown()

# Cleanup
keyboard.stop()
```

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

| Key | Event | Handler |
|-----|-------|---------|
| `<` or `,` | `ThreadControlEvent(change=-1)` | Orchestrator decreases threads |
| `>` or `.` | `ThreadControlEvent(change=+1)` | Orchestrator increases threads |
| `S` or `s` | `RequestShutdown()` | Orchestrator graceful shutdown |
| `R` or `r` | `RefreshRequested()` | Orchestrator re-scans directory |
| `C` or `c` | `ToggleOverlayTab(tab="settings")` | UIManager toggles Prefs tab |
| `F` or `f` | `ToggleOverlayTab(tab="io")` | UIManager toggles I/O tab |
| `M` or `m` | `ToggleOverlayTab(tab="shortcuts")` | UIManager toggles Keys tab |
| `D` or `d` | `ToggleOverlayTab(tab="dirs")` | UIManager toggles Dirs tab |
| `T` or `t` | `ToggleOverlayTab(tab="tui")` | UIManager toggles TUI tab |
| `E` or `e` | `ToggleOverlayTab(tab="reference")` | UIManager toggles Ref tab |
| `L` or `l` | `ToggleOverlayTab(tab="logs")` | UIManager toggles Logs tab |
| `I` or `i` | `CycleOverlayDim(direction=+1)` | UIManager cycles overlay dim level |
| `G` or `g` | `RotateGpuMetric()` | UIManager rotates the GPU metric shown in the top bar |
| `W` or `w` | `CycleSparklinePreset(direction=+1)` | UIManager cycles sparkline presets |
| `P` or `p` | `CycleSparklinePalette(direction=+1)` | UIManager cycles sparkline palettes |
| `[` / `]` | `CycleLogsPage(direction=-1/+1)` | UIManager pages Logs tab |
| `Tab` | `CycleOverlayTab(direction=+1)` | UIManager cycles tabs |
| `Esc` | `CloseOverlay()` | UIManager closes active overlay |
| `Ctrl+C` | `InterruptRequested()` | Orchestrator terminates active jobs |

## Prefs Overlay

Toggle with `C` key:

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
