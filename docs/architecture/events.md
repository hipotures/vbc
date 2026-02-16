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
**Subscribers:** UIManager
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
```

**Publisher:** Orchestrator
**Subscribers:** UIManager
**Purpose:** Report discovery results, update UI counters

### Job Lifecycle Events

#### JobStarted
```python
class JobStarted(JobEvent):
    job: CompressionJob
```

**Publisher:** Orchestrator
**Subscribers:** UIManager
**Purpose:** Add job to "Currently Processing" panel

#### JobCompleted
```python
class JobCompleted(JobEvent):
    job: CompressionJob
```

**Publisher:** Orchestrator
**Subscribers:** UIManager
**Purpose:** Update stats, move to "Last Completed" panel

#### JobFailed
```python
class JobFailed(JobEvent):
    job: CompressionJob
    error_message: str
```

**Publisher:** Orchestrator, FFmpegAdapter
**Subscribers:** UIManager
**Purpose:** Increment error counters, create .err marker

#### HardwareCapabilityExceeded
```python
class HardwareCapabilityExceeded(JobEvent):
    job: CompressionJob
```

**Publisher:** FFmpegAdapter
**Subscribers:** UIManager
**Purpose:** Track GPU capability errors separately

#### JobProgressUpdated
```python
class JobProgressUpdated(JobEvent):
    job: CompressionJob
    progress_percent: float
```

**Publisher:** FFmpegAdapter (planned)
**Subscribers:** UIManager
**Purpose:** Update progress bars (future feature)

### Queue Events

#### QueueUpdated
```python
class QueueUpdated(Event):
    pending_files: List  # List[VideoFile]
```

**Publisher:** Orchestrator
**Subscribers:** UIManager
**Purpose:** Update "Next in Queue" panel

#### RefreshRequested
```python
class RefreshRequested(Event):
    pass
```

**Publisher:** KeyboardListener
**Subscribers:** Orchestrator
**Purpose:** Re-scan directory for new files

#### RefreshFinished
```python
class RefreshFinished(Event):
    added: int = 0
    removed: int = 0
```

**Publisher:** Orchestrator
**Subscribers:** UIManager
**Purpose:** Report results after refresh completes (used for UI counters)

### Control Events

!!! note "UI/Keyboard Events Location"
    The control events below are defined in `vbc/ui/keyboard.py` rather than `vbc/domain/events.py` because they are UI-layer concerns. They still use the EventBus for communication.

#### ThreadControlEvent
```python
class ThreadControlEvent(Event):
    change: int  # +1 or -1
```

**Publisher:** KeyboardListener
**Subscribers:** Orchestrator, UIManager
**Purpose:** Adjust max concurrent threads
**Location:** `vbc/ui/keyboard.py`

#### RequestShutdown
```python
class RequestShutdown(Event):
    pass
```

**Publisher:** KeyboardListener
**Subscribers:** Orchestrator, UIManager
**Purpose:** Graceful shutdown (finish active jobs)
**Location:** `vbc/ui/keyboard.py`

#### InterruptRequested
```python
class InterruptRequested(Event):
    pass
```

**Publisher:** KeyboardListener
**Subscribers:** Orchestrator, UIManager
**Purpose:** Immediate interrupt (Ctrl+C)
**Location:** `vbc/ui/keyboard.py`

### UI Events

!!! warning "Deprecated UI Events"
    `ToggleConfig` and `HideConfig` are deprecated and replaced by the new tabbed overlay system. They remain in the codebase for backwards compatibility but will be removed in a future version.

#### ToggleOverlayTab
```python
class ToggleOverlayTab(Event):
    tab: Optional[str] = None  # "settings" | "io" | "reference" | "shortcuts" | "tui" | "logs" | None
```

**Publisher:** KeyboardListener
**Subscribers:** UIManager
**Purpose:** Toggle overlay with optional tab selection
**Location:** `vbc/ui/keyboard.py`

#### CycleLogsPage
```python
class CycleLogsPage(Event):
    direction: int = 1  # 1=next, -1=previous
```

**Publisher:** KeyboardListener
**Subscribers:** UIManager
**Purpose:** Navigate paginated entries in Logs tab
**Location:** `vbc/ui/keyboard.py`

#### CloseOverlay
```python
class CloseOverlay(Event):
    pass
```

**Publisher:** KeyboardListener
**Subscribers:** UIManager
**Purpose:** Close the overlay (Esc key)
**Location:** `vbc/ui/keyboard.py`

#### ToggleConfig (Deprecated)
```python
class ToggleConfig(Event):
    pass
```

**Publisher:** KeyboardListener
**Subscribers:** UIManager
**Purpose:** ~~Show/hide configuration overlay~~ (replaced by `ToggleOverlayTab`)
**Location:** `vbc/ui/keyboard.py`

#### HideConfig (Deprecated)
```python
class HideConfig(Event):
    pass
```

**Publisher:** KeyboardListener
**Subscribers:** UIManager
**Purpose:** ~~Close configuration overlay~~ (replaced by `CloseOverlay`)
**Location:** `vbc/ui/keyboard.py`

#### ActionMessage
```python
class ActionMessage(Event):
    message: str
```

**Publisher:** Orchestrator, KeyboardListener
**Subscribers:** UIManager
**Purpose:** User feedback messages (displayed for 60s)

### Completion Events

#### ProcessingFinished
```python
class ProcessingFinished(Event):
    pass
```

**Publisher:** Orchestrator
**Subscribers:** UIManager
**Purpose:** All jobs completed normally

## Event Flow Examples

### Job Lifecycle

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
    ┌─────────┐      subscribe      ┌───────────┐
    │EventBus │ ──────────────────>  │ UIManager │
    └────┬────┘                      └─────┬─────┘
         │                                 │
         │                                 │ 3. add_active_job()
         │                                 ▼
         │                           ┌─────────┐
         │                           │UIState  │
         │                           └─────────┘
         │
         │ 4. ffmpeg.compress()
         ▼
    ┌────────────┐
    │FFmpegAdapter│
    └──────┬─────┘
           │
           │ 5. Success → publish(JobCompleted(job))
           │    Failure → publish(JobFailed(job, error))
           │    HW Cap  → publish(HardwareCapabilityExceeded(job))
           ▼
       ┌─────────┐
       │EventBus │ ──> UIManager → UIState
       └─────────┘
```

### Keyboard Control

```
┌──────┐
│ User │
└──┬───┘
   │ Press '>'
   ▼
┌────────────────┐
│KeyboardListener│ (daemon thread)
└───────┬────────┘
        │
        │ publish(ThreadControlEvent(change=+1))
        ▼
    ┌─────────┐
    │EventBus │
    └────┬────┘
         │
         ├──> Orchestrator._on_thread_control()
         │    └─> self._current_max_threads += 1
         │        notify_all() → wake waiting threads
         │
         └──> UIManager.on_thread_control()
              └─> state.current_threads += 1
```

### Refresh Flow

```
┌──────┐
│ User │ Press 'R'
└──┬───┘
   │
   ▼
┌────────────────┐
│KeyboardListener│
└───────┬────────┘
        │ publish(RefreshRequested())
        │ publish(ActionMessage("REFRESH requested"))
        ▼
    ┌─────────┐
    │EventBus │
    └────┬────┘
         │
         ├──> Orchestrator._on_refresh_request()
         │    └─> _refresh_requested = True
         │
         └──> UIManager.on_action_message()
              └─> state.set_last_action("REFRESH requested")

(Later, in Orchestrator main loop)
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
        │ publish(ActionMessage("Refreshed: +5 new files"))
        ▼
    ┌─────────┐
    │EventBus │ ──> UIManager ──> UIState
    └─────────┘
```

## Benefits

### Loose Coupling

Components don't know about each other:

```python
# Orchestrator doesn't know about UIManager
orchestrator.run(input_dir)
# Just publishes events

# UIManager doesn't know about Orchestrator
ui_manager = UIManager(bus, state)
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
2. **Immutable events**: Treat events as immutable; don't mutate them after publish
3. **Descriptive names**: `JobCompleted` > `Event1`
4. **Minimal data**: Only include necessary fields
5. **Document purpose**: Add docstrings to event classes

## Next Steps

- [Pipeline Flow](pipeline.md) - Job processing walkthrough
- [Architecture Overview](overview.md) - High-level design
