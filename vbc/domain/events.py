from typing import Optional, List, TYPE_CHECKING
from pathlib import Path
from pydantic import BaseModel
from .models import CompressionJob

if TYPE_CHECKING:
    from .models import VideoFile

class Event(BaseModel):
    """Base class for all domain events."""
    pass

class JobEvent(Event):
    job: CompressionJob

class JobStarted(JobEvent):
    pass

class JobProgressUpdated(JobEvent):
    progress_percent: float

class JobCompleted(JobEvent):
    pass

class JobFailed(JobEvent):
    error_message: str

class HardwareCapabilityExceeded(JobEvent):
    pass

class DiscoveryStarted(Event):
    directory: Path

class DiscoveryFinished(Event):
    files_found: int
    files_to_process: int = 0
    already_compressed: int = 0
    ignored_small: int = 0
    ignored_err: int = 0
    ignored_av1: int = 0
    source_folders_count: int = 1

class QueueUpdated(Event):
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
