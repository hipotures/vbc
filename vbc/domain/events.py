"""Domain events for the video compression pipeline.

Events represent state changes and notifications that flow through the EventBus,
decoupling the pipeline orchestrator from the UI layer and enabling extensibility.

See `infrastructure/event_bus.py` for the pub/sub mechanism.
"""

from typing import List, Optional, TYPE_CHECKING
from pathlib import Path
from pydantic import BaseModel, Field
from .models import CompressionJob

if TYPE_CHECKING:
    pass


class Event(BaseModel):
    """Base class for all domain events.

    Events are validated Pydantic models. They are not frozen by default.
    """

    pass

class JobEvent(Event):
    """Base class for events related to a specific compression job."""

    job: CompressionJob


class JobStarted(JobEvent):
    """Emitted when a job begins compression."""

    pass


class JobProgressUpdated(JobEvent):
    """Emitted periodically as FFmpeg reports progress."""

    progress_percent: float


class JobCompleted(JobEvent):
    """Emitted when a job successfully completes."""

    pass


class JobFailed(JobEvent):
    """Emitted when a job fails; .err marker is written."""

    error_message: str


class HardwareCapabilityExceeded(JobEvent):
    """Emitted when GPU hardware limit is hit (HW_CAP_LIMIT status).

    Can trigger CPU fallback if configured.
    """

    pass


class DiscoveryStarted(Event):
    """Emitted when file discovery begins."""

    directory: Path


class DiscoveryErrorEntry(BaseModel):
    """Discovery-time `.err` marker details used by Logs tab."""

    path: Path
    size_bytes: Optional[int] = None
    error_message: str


class DiscoveryFinished(Event):
    """Emitted after file discovery and filtering is complete.

    Provides summary counters of discovered and filtered files.
    """

    files_found: int
    files_to_process: int = 0
    already_compressed: int = 0
    ignored_small: int = 0
    ignored_err: int = 0
    ignored_err_entries: List[DiscoveryErrorEntry] = Field(default_factory=list)
    ignored_av1: int = 0
    source_folders_count: int = 1


class QueueUpdated(Event):
    """Emitted when the processing queue changes."""

    pending_files: List  # List[VideoFile] but avoid circular import

class RefreshRequested(Event):
    """Event to trigger re-scanning for new files."""
    pass

class InputDirsChanged(Event):
    """Event emitted when active input_dirs list changes (Dirs tab apply)."""
    active_dirs: List[str]

class RefreshFinished(Event):
    """Event emitted after refresh completes (used for UI counters)."""
    added: int = 0
    removed: int = 0


class ThreadControlEvent(Event):
    """Event emitted to adjust thread count (Keys '<' or '>')."""

    change: int  # +1 or -1


class RequestShutdown(Event):
    """Event emitted when user requests graceful shutdown (Key 'S')."""

    pass


class InterruptRequested(Event):
    """Event emitted when user requests immediate interrupt (Ctrl+C)."""

    pass


class ActionMessage(Event):
    """Event for user action feedback (displayed in UI for 60s)."""
    message: str


class ProcessingFinished(Event):
    """Event emitted when all tasks finish normally."""

    pass


class WaitingForInput(Event):
    """Emitted by orchestrator when wait_on_finish=True and processing is done.
    Signals UI to display WAITING status and R/S hint."""
    pass


# ── Dirs tab events ────────────────────────────────────────────────────────────

class DirsCursorMove(Event):
    """Move cursor up/down in the Dirs tab list."""
    direction: int  # -1 = up, +1 = down


class DirsToggleSelected(Event):
    """Toggle enabled/disabled state of the entry under cursor."""
    pass


class DirsEnterAddMode(Event):
    """Enter add-path input mode in Dirs tab."""
    pass


class DirsMarkDelete(Event):
    """Mark the entry under cursor for deletion (pending)."""
    pass


class DirsInputChar(Event):
    """Append a character to the add-path input buffer.
    Use char='\\x7f' for backspace (removes last character)."""
    char: str


class DirsConfirmAdd(Event):
    """Confirm the current input buffer as a new pending directory."""
    pass


class DirsCancelInput(Event):
    """Cancel add-path input mode without saving."""
    pass


class DirsApplyChanges(Event):
    """Apply all pending Dirs changes, save to YAML, and trigger refresh."""
    pass
