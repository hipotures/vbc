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

Rich Live dashboard with 6 panels.

::: vbc.ui.dashboard
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
config = load_config()

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

## Dashboard Panels

### 1. Menu Panel
```
┌─ MENU ─────────────────────────────────────────┐
│ < decrease | > increase | S stop | R refresh  │
└────────────────────────────────────────────────┘
```

### 2. Status Panel
```
┌─ COMPRESSION STATUS ───────────────────────────┐
│ Files to compress: 12 | Already compressed: 8  │
│ Ignored: size:5 | err:2 | hw_cap:1 | av1:3    │
│ Total: 12 files | Threads: 4 | 500MB → 180MB  │
│ ETA: 00h 15m (based on 8.5MB/s throughput)    │
└────────────────────────────────────────────────┘
```

### 3. Progress Panel
```
┌────────────────────────────────────────────────┐
│ Progress: 8/12 (66%) | Active threads: 4      │
│ Last action: Threads: 4 → 8                   │
└────────────────────────────────────────────────┘
```

### 4. Currently Processing Panel
```
┌─ CURRENTLY PROCESSING ─────────────────────────┐
│ ◐ video1.mp4  8M 60fps  120.5MB  00:15        │
│ ◓ video2.mov  4M 30fps   85.2MB  00:08        │
│ ◑ video3.avi  2M 24fps   45.8MB  00:05        │
└────────────────────────────────────────────────┘
```

Spinner types:
- `◐◓◑◒` - Rotating spinner (with rotation applied)
- `●○◉◎` - Simple circles (no rotation)

### 5. Last Completed Panel
```
┌─ LAST COMPLETED ───────────────────────────────┐
│ ✓ video1.mp4  8M 60fps  120MB → 42MB  65.0%  │
│ ✓ video2.mov  4M 30fps   85MB → 32MB  62.3%  │
│ ✓ video3.avi  2M 24fps   45MB → 18MB  60.0%  │
└────────────────────────────────────────────────┘
```

### 6. Next in Queue Panel
```
┌─ NEXT IN QUEUE ────────────────────────────────┐
│ » video4.mp4  8M 60fps  ILCE-7RM5  95.2MB    │
│ » video5.mov  4M 30fps  DC-GH7     78.5MB    │
│ » video6.avi  2M 24fps  Unknown    52.1MB    │
└────────────────────────────────────────────────┘
```

### 7. Summary Panel
```
┌────────────────────────────────────────────────┐
│ ✓ 8 success  ✗ 2 errors  ⚠ 1 hw_cap  ⊘ 0 skip│
└────────────────────────────────────────────────┘
```

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
    "Dynamic CQ: ILCE-7RM5:38, DC-GH7:40",
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
| `C` or `c` | `ToggleConfig()` | UIManager toggles config overlay |
| `Esc` | `HideConfig()` | UIManager hides config overlay |
| `Ctrl+C` | `InterruptRequested()` | Orchestrator terminates active jobs |

## Configuration Overlay

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
│ Dynamic CQ: ILCE-7RM5:38, DC-GH7:40           │
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
