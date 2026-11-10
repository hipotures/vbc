# Event System

VBC uses an event-driven architecture with a synchronous Pub/Sub event bus for decoupled communication.

## Design

### EventBus

Simple synchronous event dispatcher:

```python
class EventBus:
    def __init__(self):
        self._subscribers: Dict[Type[Event], List[Callable]] = {}

    def subscribe(self, event_type: Type[Event], handler: Callable):
        """Subscribe to an event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)

    def publish(self, event: Event):
        """Publish an event to all subscribers."""
        event_type = type(event)
        if event_type in self._subscribers:
            for handler in self._subscribers[event_type]:
                handler(event)
```

**Characteristics:**
- **Synchronous**: Handlers execute in publish thread
- **Ordered**: Subscribers called in registration order
- **Type-safe**: Pydantic events ensure data validity

## Event Types

### Discovery Events

#### DiscoveryStarted
```python
class DiscoveryStarted(Event):
    directory: Path
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Signal start of file scanning

#### DiscoveryFinished
```python
class DiscoveryFinished(Event):
    files_found: int
    files_to_process: int = 0
    already_compressed: int = 0
    ignored_small: int = 0
    ignored_err: int = 0
    ignored_av1: int = 0
    ignored_err_entries: List[DiscoveryErrorEntry] = Field(default_factory=list)
    source_folders_count: int = 1
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Report discovery results, update state counters, show ignored `.err` entries, and track multi-source discovery

### Job Lifecycle Events

#### JobStarted
```python
class JobStarted(JobEvent):
    job: CompressionJob
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Add job to "Currently Processing" panel

#### JobCompleted
```python
class JobCompleted(JobEvent):
    job: CompressionJob
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Update stats, move to "Last Completed" panel

#### JobFailed
```python
class JobFailed(JobEvent):
    job: CompressionJob
    error_message: str
```

**Publisher:** Orchestrator, FFmpegAdapter
**Subscribers:** StateStore
**Purpose:** Increment error counters, create .err marker

#### HardwareCapabilityExceeded
```python
class HardwareCapabilityExceeded(JobEvent):
    job: CompressionJob
```

**Publisher:** FFmpegAdapter
**Subscribers:** StateStore
**Purpose:** Track GPU capability errors separately

#### JobProgressUpdated
```python
class JobProgressUpdated(JobEvent):
    job: CompressionJob
    progress_percent: float
```

**Publisher:** FFmpegAdapter
**Subscribers:** StateStore
**Purpose:** Update per-job progress in the active jobs panel

### Queue Events

#### QueueUpdated
```python
class QueueUpdated(Event):
    pending_files: List  # List[VideoFile]
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Update pending files queue

#### RefreshRequested
```python
class RefreshRequested(Event):
    manifest_paths: List[Path] = Field(default_factory=list)
    automatic: bool = False
    input_dirs: List[Path] = Field(default_factory=list)
```

**Publisher:** CommandService (`POST /api/v1/refresh`), ManifestWatcher, PollingWatcher
**Subscribers:** Orchestrator, VbcServer
**Purpose:** Re-scan all or specific directories, or incrementally discover ready manifests. `automatic=True` (from watchers or Dirs edits) never resumes a stopped server; only an explicit refresh (`automatic=False`) does.

#### RefreshFinished
```python
class RefreshFinished(Event):
    added: int = 0
    removed: int = 0
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Report results after refresh completes (used for counters)

#### SourcesDeleted
```python
class SourcesDeleted(Event):
    count: int
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Count sources deleted outside a job, when discovery finds a manifest's output already verified and applies its source policy. Deletions by a job travel in `JobCompleted` (`job.sources_deleted`).

#### InputDirsChanged
```python
class InputDirsChanged(Event):
    active_dirs: List[str]
```

**Publisher:** CommandService (`PUT /api/v1/dirs`)
**Subscribers:** Orchestrator, VbcServer, Watchers
**Purpose:** Update active input directory set and rebuild watchers

### Server Control Events (`vbc/domain/events.py`)

!!! note "Absolute & Idempotent Control Events"
    Server control events in `vbc/domain/events.py` use absolute targets and explicit booleans rather than relative deltas or toggles, making network retries and concurrent clients safe.

#### SetThreadCount
```python
class SetThreadCount(Event):
    target: int
```

**Publisher:** CommandService (`PUT /api/v1/threads`)
**Subscribers:** Orchestrator
**Purpose:** Set max concurrent threads to an absolute target (clamped to `1..max(general.max_threads, general.threads)`)

#### ThreadCountChanged
```python
class ThreadCountChanged(Event):
    current: int
    minimum: int
    maximum: int
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Report the effective (clamped) thread count and bounds

#### SetShutdownRequested
```python
class SetShutdownRequested(Event):
    requested: bool = True
```

**Publisher:** CommandService (`POST /api/v1/stop`, `DELETE /api/v1/stop`), VbcServer (on `SIGTERM`/`SIGINT`)
**Subscribers:** Orchestrator, StateStore
**Purpose:** Request (`True`) or cancel (`False`) a graceful stop (active jobs finish, no new jobs start)

#### InterruptRequested
```python
class InterruptRequested(Event):
    pass
```

**Publisher:** CommandService (`POST /api/v1/stop {"mode": "interrupt"}`), VbcServer (second `SIGINT` or stop timeout deadline)
**Subscribers:** Orchestrator, StateStore
**Purpose:** Immediate interrupt of active jobs

#### ActionMessage
```python
class ActionMessage(Event):
    message: str
```

**Publisher:** Orchestrator, CommandService
**Subscribers:** StateStore
**Purpose:** User feedback messages (displayed for 60s)

### Completion Events (`vbc/domain/events.py`)

#### ProcessingFinished
```python
class ProcessingFinished(Event):
    pass
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Pass jobs completed (emitted before automatic repair / error file move)

#### ProcessingPausedOnError
```python
class ProcessingPausedOnError(Event):
    message: str
    reason: str = "verification_failed"
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Pause processing after a verification failure and enter `paused_on_error` state

#### RepairStarted
```python
class RepairStarted(Event):
    candidate_count: int = 0
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Enter `repairing` state before repairing current-session failures

#### RepairFinished
```python
class RepairFinished(Event):
    attempted: int = 0
    repaired: int = 0
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Leave `repairing` state and record repair summary

#### WaitingForInput
```python
class WaitingForInput(Event):
    pass
```

**Publisher:** Orchestrator
**Subscribers:** StateStore
**Purpose:** Pass and post-processing finished; server enters `idle` state and waits for refresh or watcher events

### TUI Client Events (`vbc/clients/tui/`)

In `vbc tui`, keyboard and overlay view events travel on a client-local `EventBus` (`vbc/clients/tui/bus.py`) from `KeyboardListener` (`vbc/clients/tui/keyboard.py`) to `TuiController` (`vbc/clients/tui/controller.py`). View events mutate `ClientState` locally; command events call the server API on a background worker thread.

#### Client Command Events (`vbc/clients/tui/events.py`)
- `ThreadControlEvent(change: int)`: `<` / `>` keys; `TuiController` computes `current_threads + change` and calls `PUT /api/v1/threads`.
- `StopRequested()`: `S` key; calls `POST /api/v1/stop` (graceful) or `DELETE /api/v1/stop` if already stopping.
- `RefreshRequested()`: `R` key; calls `POST /api/v1/refresh`.
- `QuitRequested()`: `Q`, `Ctrl+Q`, or `Ctrl+C`; detaches the TUI client immediately (`POST /api/v1/clients/goodbye`).
- `ActionMessage(message: str)`: Sets a local feedback message in `ClientState`.

#### Overlay & Sparkline Events (`vbc/clients/tui/keyboard.py`)
- `ToggleOverlayTab(tab: Optional[str])`: Open/toggle overlay tab (`"settings"`, `"io"`, `"dirs"`, `"reference"`, `"shortcuts"`, `"tui"`, `"logs"`).
- `CycleOverlayTab(direction: int = 1)`: Cycle tabs (`Tab`).
- `CloseOverlay()`: Close active overlay (`Esc`).
- `CycleLogsPage(direction: int = 1)`: Navigate pages in the Logs tab (`[` / `]`).
- `CycleOverlayDim(direction: int = 1)`: Cycle background dim level (`I` in TUI tab).
- `RotateGpuMetric()`: Rotate GPU/CPU metric shown in the header (`G`).
- `CycleSparklinePreset(direction: int = 1)`: Cycle sparkline preset (`W` in TUI tab).
- `CycleSparklinePalette(direction: int = 1)`: Cycle sparkline color palette (`O` in TUI tab).

#### Dirs Tab Editor Events (`vbc/clients/tui/events.py`)
- `DirsCursorMove(direction: int)`: Move selection cursor (`↑` / `↓`).
- `DirsSwapSelected(direction: int)`: Reorder selected directory (`Shift+↑` / `Shift+↓`).
- `DirsToggleSelected()`: Stage enable/disable toggle (`Space`).
- `DirsEnterAddMode()`: Enter path input mode (`A`).
- `DirsMarkDelete()`: Mark/unmark selected directory for deletion (`Delete` / `Backspace`).
- `DirsInputChar(char: str)`: Append character or backspace in add-path buffer.
- `DirsConfirmAdd()`: Validate and stage new path (`Enter`).
- `DirsCancelInput()`: Exit add-path input mode (`Esc`).
- `DirsApplyChanges()`: Send full staged directory list via `PUT /api/v1/dirs` with `If-Match` (`S`).
- `DirsScanMounts()`: Request server mount scan via `POST /api/v1/dirs/scan` (`R` in Dirs tab).

## Event Flow Examples

### Job Lifecycle (`vbc serve`)

```
┌─────────────┐
│Orchestrator │
└──────┬──────┘
       │
       │ 1. Submit job to ThreadPoolExecutor
       ▼
┌────────────────┐
│ _process_file()│
└───────┬────────┘
        │ 2. Create CompressionJob
        │
        │ publish(JobStarted(job))
        ▼
    ┌─────────┐      subscribe      ┌────────────┐
    │EventBus │ ──────────────────> │ StateStore │
    └────┬────┘                     └─────┬──────┘
         │                                │
         │                                │ 3. Update active jobs & bump rev
         │                                ▼
         │                          ┌──────────────────┐
         │                          │ Long-poll wakes  │
         │                          │ /api/v1/state    │
         │                          └──────────────────┘
         │
         │ 4. ffmpeg.compress()
         ▼
    ┌─────────────┐
    │FFmpegAdapter│
    └──────┬──────┘
           │
           │ 5. Progress → publish(JobProgressUpdated) (re-anchors when drift > 2 p.p.)
           │    Success  → publish(JobCompleted(job))
           │    Failure  → publish(JobFailed(job, error))
           │    HW Cap   → publish(HardwareCapabilityExceeded(job))
           ▼
       ┌─────────┐
       │EventBus │ ──> StateStore (updates counters/anchors, bumps rev)
       └─────────┘
```

### Runtime Thread Control (`vbc tui` → `vbc serve`)

```
┌──────┐
│ User │ Press '>' in vbc tui
└──┬───┘
   │
   ▼
┌────────────────┐
│KeyboardListener│ (client bus)
└───────┬────────┘
        │ publish(ThreadControlEvent(change=+1))
        ▼
┌────────────────┐
│ TuiController  │
└───────┬────────┘
        │ PUT /api/v1/threads {"target": current + 1}
        ▼
┌────────────────┐
│ CommandService │ (server)
└───────┬────────┘
        │ publish(SetThreadCount(target=5))
        ▼
    ┌─────────┐
    │EventBus │
    └────┬────┘
         │
         └──> Orchestrator._on_thread_control()
              ├─> clamp to 1..max(max_threads, threads)
              ├─> notify_all() → wake waiting worker threads
              ├─> publish(ThreadCountChanged(current=5, minimum=1, maximum=8))
              │     └──> StateStore updates threads & bumps rev
              └──> publish(ActionMessage("Threads: 4 → 5"))
                    └──> StateStore updates last_action
```

### Refresh Flow

```
┌─────────────────┐
│ Client / Watcher│ POST /api/v1/refresh or filesystem event
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ CommandService  │
│  / PollingWatcher│
└────────┬────────┘
         │ publish(RefreshRequested())
         │ publish(ActionMessage("REFRESH requested"))
         ▼
     ┌─────────┐
     │EventBus │
     └────┬────┘
          │
          ├──> Orchestrator._on_refresh_request()
          │    └─> _refresh_requested = True (wakes wait loop)
          │
          └──> StateStore.on_action_message()
               └─> last_action = "REFRESH requested"

(In Orchestrator main loop)
┌────────────────┐
│ Orchestrator   │
└───────┬────────┘
        │ Check _refresh_requested
        │
        │ Re-scan directory
        ▼
    _perform_discovery()
        │
        │ publish(DiscoveryFinished(...))
        │ publish(QueueUpdated(...))
        │ publish(ActionMessage("Refreshed: +5 new files"))
        ▼
    ┌─────────┐
    │EventBus │ ──> StateStore (bumps rev)
    └─────────┘
```

## Benefits

### Loose Coupling

Components don't know about each other:

```python
# Orchestrator doesn't know about StateStore or HTTP clients
orchestrator.run(input_dirs)
# Just publishes events

# StateStore doesn't know about Orchestrator
state_store = StateStore(bus, ...)
# Just subscribes to events
```

**Benefit:** Can replace/remove components without changing others.

### Testability

Easy to mock EventBus:

```python
def test_orchestrator():
    bus = Mock()
    orchestrator = Orchestrator(..., event_bus=bus)
    orchestrator.run(test_dir)

    # Verify events published
    bus.publish.assert_any_call(DiscoveryStarted(...))
    bus.publish.assert_any_call(JobStarted(...))
```

### Extensibility

Add new subscribers without modifying publishers:

```python
# Add webhook notifier (no changes to Orchestrator)
class WebhookNotifier:
    def __init__(self, bus, url):
        self.url = url
        bus.subscribe(JobCompleted, self.on_job_completed)

    def on_job_completed(self, event):
        requests.post(self.url, json={"job": event.job.dict()})

# Just instantiate
webhook = WebhookNotifier(bus, "https://example.com/webhook")
```

### Debugging

Enable event logging:

```python
class EventLogger:
    def __init__(self, bus):
        # Subscribe to ALL events
        for event_type in Event.__subclasses__():
            bus.subscribe(event_type, self.log)

    def log(self, event):
        print(f"[EVENT] {type(event).__name__}: {event}")

logger = EventLogger(bus)
```

## Trade-offs

### Synchronous Execution

**Pro:** Simple, predictable order
**Con:** Slow handlers block publisher

**Mitigation:** Keep handlers fast (just update state, no I/O)

### Error Handling

**Issue:** Exception in handler crashes publisher

**Solution:** Wrap publish in try/except:

```python
def publish(self, event: Event):
    for handler in self._subscribers.get(type(event), []):
        try:
            handler(event)
        except Exception as e:
            logger.error(f"Event handler failed: {e}")
```

### Type Safety

**Pro:** Pydantic validates event data
**Con:** Runtime overhead (minimal)

**Benefit:** Catch bugs early (e.g., missing fields, wrong types)

## Best Practices

1. **One event per action**: Don't combine unrelated state changes
2. **Treat events as value objects**: Avoid mutating event fields after publish (recommended convention, not enforced by Pydantic config)
3. **Descriptive names**: `JobCompleted` > `Event1`
4. **Minimal data**: Only include necessary fields
5. **Document purpose**: Add docstrings to event classes

## Next Steps

- [Pipeline Flow](pipeline.md) - Job processing walkthrough
- [Architecture Overview](overview.md) - High-level design
