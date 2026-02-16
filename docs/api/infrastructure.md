# Infrastructure API

This page documents the infrastructure adapters that interact with external systems.

## Event Bus

Synchronous Pub/Sub event system for decoupled communication.

::: vbc.infrastructure.event_bus
    options:
      show_source: true
      heading_level: 3

## File Scanner

Recursive directory scanner with filtering.

::: vbc.infrastructure.file_scanner
    options:
      show_source: true
      heading_level: 3

## ExifTool Adapter

Wrapper around pyexiftool for metadata extraction.

::: vbc.infrastructure.exif_tool
    options:
      show_source: true
      heading_level: 3

## FFprobe Adapter

Wrapper around ffprobe for stream information.

::: vbc.infrastructure.ffprobe
    options:
      show_source: true
      heading_level: 3

## FFmpeg Adapter

Wrapper around ffmpeg for video compression.

::: vbc.infrastructure.ffmpeg
    options:
      show_source: true
      heading_level: 3

## Housekeeping Service

Cleanup service for temporary files and error markers.

::: vbc.infrastructure.housekeeping
    options:
      show_source: true
      heading_level: 3

## Logging

Logging configuration and setup.

::: vbc.infrastructure.logging
    options:
      show_source: true
      heading_level: 3

## Usage Examples

### EventBus

```python
from vbc.infrastructure.event_bus import EventBus
from vbc.domain.events import Event
from pydantic import BaseModel

# Create custom event
class CustomEvent(Event):
    message: str

# Create bus
bus = EventBus()

# Subscribe
def handler(event: CustomEvent):
    print(f"Received: {event.message}")

bus.subscribe(CustomEvent, handler)

# Publish
bus.publish(CustomEvent(message="Hello!"))
# Output: Received: Hello!
```

### FileScanner

```python
from pathlib import Path
from vbc.infrastructure.file_scanner import FileScanner

# Create scanner
scanner = FileScanner(
    extensions=[".mp4", ".mov", ".avi"],
    min_size_bytes=1024 * 1024  # 1 MiB
)

# Scan directory
for video_file in scanner.scan(Path("/videos")):
    print(f"{video_file.path.name}: {video_file.size_bytes} bytes")
```

### ExifToolAdapter

```python
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.domain.models import VideoFile
from pathlib import Path

# Create adapter
exif = ExifToolAdapter()
exif.et.run()  # Start ExifTool process

# Extract metadata
video = VideoFile(path=Path("video.mp4"), size_bytes=1000000)
metadata = exif.extract_metadata(video)

print(f"Camera: {metadata.camera_model}")
print(f"Resolution: {metadata.width}x{metadata.height}")
print(f"Codec: {metadata.codec}")

# Extract EXIF info with dynamic quality
dynamic_quality = {
    "ILCE-7RM5": {"cq": 38, "rate": {"bps": "0.8", "minrate": "0.7", "maxrate": "0.9"}},
    "DC-GH7": {"cq": 40},
}
exif_info = exif.extract_exif_info(video, dynamic_quality)

if exif_info["custom_cq"]:
    print(f"Using custom CQ: {exif_info['custom_cq']}")

# Copy metadata
exif.copy_metadata(
    source=Path("source.mp4"),
    target=Path("output.mp4")
)

# Cleanup
exif.et.terminate()
```

### FFprobeAdapter

```python
from vbc.infrastructure.ffprobe import FFprobeAdapter
from pathlib import Path

# Create adapter
ffprobe = FFprobeAdapter()

# Get stream info
info = ffprobe.get_stream_info(Path("video.mp4"))

print(f"Codec: {info['codec']}")
print(f"Resolution: {info['width']}x{info['height']}")
print(f"FPS: {info['fps']}")
print(f"Color space: {info['color_space']}")
print(f"Duration: {info['duration']} seconds")
```

### FFmpegAdapter

```python
from vbc.infrastructure.ffmpeg import FFmpegAdapter
from vbc.infrastructure.event_bus import EventBus
from vbc.domain.models import CompressionJob, VideoFile, JobStatus
from vbc.config.models import AppConfig, GeneralConfig
from pathlib import Path
import threading

# Create adapter
bus = EventBus()
ffmpeg = FFmpegAdapter(event_bus=bus)

# Create job
job = CompressionJob(
    source_file=VideoFile(
        path=Path("input.mp4"),
        size_bytes=100000000
    ),
    output_path=Path("output.mp4"),
    rotation_angle=180
)

# Create config
config = AppConfig(
    general=GeneralConfig(threads=4, gpu=True, copy_metadata=True)
)

# Compress
shutdown_event = threading.Event()
ffmpeg.compress(
    job=job,
    config=config,
    use_gpu=config.general.gpu,
    rotate=180,
    shutdown_event=shutdown_event
)

# Check status
if job.status == JobStatus.COMPLETED:
    print(f"Success! Output: {job.output_path}")
elif job.status == JobStatus.HW_CAP_LIMIT:
    print("Hardware capability exceeded")
elif job.status == JobStatus.FAILED:
    print(f"Failed: {job.error_message}")
```

### HousekeepingService

```python
from vbc.infrastructure.housekeeping import HousekeepingService
from pathlib import Path

# Create service
housekeeper = HousekeepingService()

# Cleanup markers in output dir
housekeeper.cleanup_output_markers(
    input_dir=Path("/videos"),
    output_dir=Path("/videos_out"),
    errors_dir=Path("/videos_err"),
    clean_errors=True,   # True: cleanup .tmp and .err, False: only .tmp
)
# Removes or relocates markers:
# - *.tmp always
# - *.err only when clean_errors=True
```

## Adapter Patterns

### Dependency Injection

All adapters are injected into the Orchestrator:

```python
from pathlib import Path
from vbc.pipeline.orchestrator import Orchestrator
from vbc.infrastructure.file_scanner import FileScanner
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.infrastructure.ffprobe import FFprobeAdapter
from vbc.infrastructure.ffmpeg import FFmpegAdapter
from vbc.infrastructure.event_bus import EventBus
from vbc.config.loader import load_config

# Create dependencies
config = load_config(Path("conf/vbc.yaml"))
bus = EventBus()
scanner = FileScanner(
    extensions=config.general.extensions,
    min_size_bytes=config.general.min_size_bytes
)
exif = ExifToolAdapter()
ffprobe = FFprobeAdapter()
ffmpeg = FFmpegAdapter(event_bus=bus)

# Inject into orchestrator
orchestrator = Orchestrator(
    config=config,
    event_bus=bus,
    file_scanner=scanner,
    exif_adapter=exif,
    ffprobe_adapter=ffprobe,
    ffmpeg_adapter=ffmpeg
)

# Run
orchestrator.run(Path("/videos"))
```

### Thread Safety

Adapters that access shared state use locks:

```python
# ExifToolAdapter uses threading.Lock
class ExifToolAdapter:
    def __init__(self):
        self.et = exiftool.ExifTool()
        self._lock = threading.Lock()

    def extract_metadata(self, file: VideoFile):
        with self._lock:
            metadata = self.et.execute_json(str(file.path))
        # ... process metadata
```

### Error Handling

Adapters raise exceptions for invalid inputs:

```python
from vbc.infrastructure.ffprobe import FFprobeAdapter

ffprobe = FFprobeAdapter()

try:
    info = ffprobe.get_stream_info(Path("nonexistent.mp4"))
except Exception as e:
    print(f"ffprobe failed: {e}")
    # Handle corrupted/missing file
```
