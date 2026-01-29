"""Domain events for the video compression pipeline.

Events represent state changes and notifications that flow through the EventBus,
decoupling the pipeline orchestrator from the UI layer and enabling extensibility.

See `infrastructure/event_bus.py` for the pub/sub mechanism.
"""

from typing import List, TYPE_CHECKING
from pathlib import Path
from pydantic import BaseModel
from .models import CompressionJob

if TYPE_CHECKING:
    pass


class Event(BaseModel):
    """Base class for all domain events.

    All events are immutable Pydantic models, automatically validated.
    """

    pass

class JobEvent(Event):
    """Base class for events related to a specific compression job."""

    job: CompressionJob


class JobStarted(JobEvent):
    """Emitted when a job begins compression."""

    pass


class JobProgressUpdated(JobEvent):
    """Emitted periodically as FFmpeg reports progress (future feature)."""

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


class DiscoveryFinished(Event):
    """Emitted after file discovery and filtering is complete.

    Provides summary counters of discovered and filtered files.
    """

    files_found: int
    files_to_process: int = 0
    already_compressed: int = 0
    ignored_small: int = 0
    ignored_err: int = 0
    ignored_av1: int = 0
    source_folders_count: int = 1


class QueueUpdated(Event):
    """Emitted when the processing queue changes."""

    pending_files: List  # List[VideoFile] but avoid circular import

class RefreshRequested(Event):
    """Event to trigger re-scanning for new files."""
    pass

class RefreshFinished(Event):
    """Event emitted after refresh completes (used for UI counters)."""
    added: int = 0
    removed: int = 0

class ActionMessage(Event):
    """Event for user action feedback (displayed in UI for 60s)."""
    message: str

class ProcessingFinished(Event):
    """Event emitted when all tasks finish normally."""
    pass
