# Domain API

This page documents the core domain models and events.

## Models

Domain models are framework-agnostic business entities.

::: vbc.domain.models
    options:
      show_source: true
      heading_level: 3

## Events

Domain events enable decoupled communication between components.

::: vbc.domain.events
    options:
      show_source: true
      heading_level: 3

## Usage Examples

### Working with VideoFile

```python
from pathlib import Path
from vbc.domain.models import VideoFile, VideoMetadata

# Create a VideoFile
video = VideoFile(
    path=Path("/videos/sample.mp4"),
    size_bytes=125829120  # 120 MB
)

# Add metadata after extraction
video.metadata = VideoMetadata(
    width=1920,
    height=1080,
    codec="h264",
    fps=60.0,
    camera_model="ILCE-7RM5",
    bitrate_kbps=15000.0
)

# Access metadata
print(f"Resolution: {video.metadata.width}x{video.metadata.height}")
print(f"Camera: {video.metadata.camera_model}")
```

### Working with CompressionJob

```python
from vbc.domain.models import CompressionJob, JobStatus, VideoFile
from pathlib import Path

# Create a job
job = CompressionJob(
    source_file=VideoFile(
        path=Path("/videos/sample.mp4"),
        size_bytes=125829120
    ),
    output_path=Path("/videos_out/sample.mp4"),
    rotation_angle=180
)

# Initial status
assert job.status == JobStatus.PENDING

# Update during processing
job.status = JobStatus.PROCESSING

# Mark as completed
job.status = JobStatus.COMPLETED
job.output_size_bytes = 45891200  # 43 MB
job.duration_seconds = 127.5

# Calculate compression ratio
ratio = job.output_size_bytes / job.source_file.size_bytes
savings = (1 - ratio) * 100
print(f"Compression: {savings:.1f}% savings")  # 63.5% savings
```

### Using Events

```python
from vbc.infrastructure.event_bus import EventBus
from vbc.domain.events import JobStarted, JobCompleted
from vbc.domain.models import CompressionJob, VideoFile
from pathlib import Path

# Create event bus
bus = EventBus()

# Subscribe to events
def on_job_started(event: JobStarted):
    print(f"Job started: {event.job.source_file.path.name}")

def on_job_completed(event: JobCompleted):
    print(f"Job completed: {event.job.source_file.path.name}")

bus.subscribe(JobStarted, on_job_started)
bus.subscribe(JobCompleted, on_job_completed)

# Publish events
job = CompressionJob(
    source_file=VideoFile(path=Path("video.mp4"), size_bytes=1000000)
)

bus.publish(JobStarted(job=job))
# Output: Job started: video.mp4

bus.publish(JobCompleted(job=job))
# Output: Job completed: video.mp4
```

### Job Status Transitions

```python
from vbc.domain.models import JobStatus

# Valid transitions
status = JobStatus.PENDING
status = JobStatus.PROCESSING
status = JobStatus.COMPLETED

# Error states
status = JobStatus.FAILED        # Generic failure
status = JobStatus.HW_CAP_LIMIT  # Hardware capability exceeded
status = JobStatus.SKIPPED       # Skipped (AV1, camera filter, etc.)
status = JobStatus.INTERRUPTED   # User interrupted (Ctrl+C)
```

## Event Reference

| Event | Trigger | Data | Use Case |
|-------|---------|------|----------|
| `DiscoveryStarted` | Orchestrator starts scanning | `directory: Path` | UI: Show "Scanning..." |
| `DiscoveryFinished` | Scanning complete | File counts | UI: Update totals |
| `JobStarted` | Job begins processing | `job: CompressionJob` | UI: Add to active list |
| `JobCompleted` | Job finishes successfully | `job: CompressionJob` | UI: Update stats |
| `JobFailed` | Job fails | `job`, `error_message` | UI: Increment errors |
| `HardwareCapabilityExceeded` | GPU lacks capability | `job` | UI: Show HW_CAP count |
| `JobProgressUpdated` | FFmpeg progress update | `progress_percent` | UI: Update progress bar |
| `QueueUpdated` | Pending files changed | `pending_files: List` | UI: Show next in queue |
| `ActionMessage` | User action feedback | `message: str` | UI: Show for 60s |
| `ProcessingFinished` | All jobs done | - | UI: Show completion |
| `RefreshRequested` | User pressed 'R' | - | Orchestrator: Re-scan |
| `ThreadControlEvent` | User pressed `<`/`>` | `change: int` | Orchestrator: Adjust threads |
| `RequestShutdown` | User pressed 'S' | - | Orchestrator: Stop accepting jobs |
| `InterruptRequested` | User pressed Ctrl+C | - | Orchestrator: Terminate active jobs |
