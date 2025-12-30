# Pipeline API

This page documents the Orchestrator, the core processing coordinator.

## Orchestrator

The Orchestrator is the heart of VBC, coordinating all compression jobs.

::: vbc.pipeline.orchestrator
    options:
      show_source: true
      heading_level: 3
      members:
        - Orchestrator
        - Orchestrator.__init__
        - Orchestrator.run

## Key Responsibilities

1. **Discovery**: Scan input directory and filter files
2. **Metadata Management**: Thread-safe caching of video metadata
3. **Decision Logic**: Determine CQ and rotation per file
4. **Queue Management**: Submit-on-demand pattern with prefetch
5. **Concurrency Control**: Dynamic thread adjustment
6. **Event Emission**: Publish events for UI updates
7. **Error Handling**: Create .err markers, handle corrupted files
8. **Graceful Shutdown**: Finish active jobs on user request

## Usage Example

```python
from pathlib import Path
from vbc.config.loader import load_config
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.file_scanner import FileScanner
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.infrastructure.ffprobe import FFprobeAdapter
from vbc.infrastructure.ffmpeg import FFmpegAdapter
from vbc.pipeline.orchestrator import Orchestrator

# Load configuration
config = load_config(Path("conf/vbc.yaml"))

# Create dependencies
bus = EventBus()
scanner = FileScanner(
    extensions=config.general.extensions,
    min_size_bytes=config.general.min_size_bytes
)
exif = ExifToolAdapter()
exif.et.run()  # Start ExifTool process

ffprobe = FFprobeAdapter()
ffmpeg = FFmpegAdapter(event_bus=bus)

# Create orchestrator
orchestrator = Orchestrator(
    config=config,
    event_bus=bus,
    file_scanner=scanner,
    exif_adapter=exif,
    ffprobe_adapter=ffprobe,
    ffmpeg_adapter=ffmpeg
)

# Run compression
try:
    orchestrator.run(Path("/videos"))
finally:
    # Cleanup ExifTool
    if exif.et.running:
        exif.et.terminate()
```

## Processing Pipeline

### 1. Discovery Phase

```python
# Orchestrator scans directory
files_to_process, stats = orchestrator._perform_discovery(input_dir)

# Emits event
bus.publish(DiscoveryFinished(
    files_found=stats['files_found'],
    files_to_process=stats['files_to_process'],
    already_compressed=stats['already_compressed'],
    ignored_small=stats['ignored_small'],
    ignored_err=stats['ignored_err']
))
```

### 2. Job Processing

```python
# For each file
def _process_file(video_file: VideoFile, input_dir: Path):
    # 1. Check for existing .err marker
    if err_path.exists() and not config.general.clean_errors:
        return  # Skip

    # 2. Get stream info (ffprobe)
    stream_info = ffprobe_adapter.get_stream_info(video_file.path)

    # 3. Check and fix color space if needed
    input_path, temp_fixed = _check_and_fix_color_space(
        video_file.path,
        output_path,
        stream_info
    )

    # 4. Get metadata (ExifTool, cached)
    video_file.metadata = _get_metadata(video_file, stream_info)

    # 5. Filter checks
    if config.general.skip_av1 and metadata.codec == "av1":
        return  # Skip

    if config.general.filter_cameras:
        if camera_model not in filter_cameras:
            return  # Skip

    # 6. Determine CQ and rotation
    target_cq = _determine_cq(video_file)
    rotation = _determine_rotation(video_file)

    # 7. Create job and compress
    job = CompressionJob(
        source_file=video_file,
        output_path=output_path,
        rotation_angle=rotation
    )
    bus.publish(JobStarted(job=job))

    # 8. Compress
    ffmpeg_adapter.compress(job, config, rotate=rotation)

    # 9. Post-processing
    if job.status == JobStatus.COMPLETED:
        # Copy metadata
        _copy_deep_metadata(source, output)

        # Check min ratio
        ratio = output_size / input_size
        if ratio > (1.0 - config.general.min_compression_ratio):
            shutil.copy2(source, output)  # Keep original

        bus.publish(JobCompleted(job=job))
    else:
        # Write .err file
        err_path.write_text(job.error_message)
        bus.publish(JobFailed(job=job))
```

## Concurrency Control

### ThreadController Pattern

```python
# Block until thread slot available
with self._thread_lock:
    while self._active_threads >= self._current_max_threads:
        self._thread_lock.wait()

    if self._shutdown_requested:
        return  # Don't start new jobs

    self._active_threads += 1

# Process job...

# Release slot
with self._thread_lock:
    self._active_threads -= 1
    self._thread_lock.notify_all()  # Wake up waiting threads
```

### Dynamic Adjustment

```python
# User presses '>' key
def _on_thread_control(self, event: ThreadControlEvent):
    with self._thread_lock:
        old = self._current_max_threads
        new = old + event.change
        self._current_max_threads = max(1, min(16, new))
        self._thread_lock.notify_all()  # Wake up waiting threads

    bus.publish(ActionMessage(message=f"Threads: {old} → {new}"))
```

## Submit-on-Demand Pattern

```python
from collections import deque

pending = deque(files_to_process)
in_flight = {}  # future -> VideoFile

def submit_batch():
    """Submit files up to max_inflight limit"""
    max_inflight = config.general.prefetch_factor * current_max_threads
    while len(in_flight) < max_inflight and pending:
        vf = pending.popleft()
        future = executor.submit(_process_file, vf, input_dir)
        in_flight[future] = vf

    # Update UI with pending files
    bus.publish(QueueUpdated(pending_files=list(pending)))

# Submit initial batch
submit_batch()

# Process as they complete
while in_flight:
    done, _ = wait(in_flight, timeout=1.0, return_when=FIRST_COMPLETED)

    for future in done:
        future.result()
        del in_flight[future]

    # Replenish queue
    submit_batch()
```

## Metadata Caching

```python
# Thread-safe cache to avoid redundant ExifTool calls
_metadata_cache: Dict[Path, VideoMetadata] = {}
_metadata_lock = threading.Lock()

def _get_metadata(video_file: VideoFile) -> VideoMetadata:
    with self._metadata_lock:
        cached = self._metadata_cache.get(video_file.path)
        if cached:
            return cached

    # Extract metadata
    stream_info = ffprobe_adapter.get_stream_info(video_file.path)
    metadata = _build_metadata(video_file, stream_info)

    # Cache it
    with self._metadata_lock:
        self._metadata_cache[video_file.path] = metadata

    return metadata
```

## Decision Logic

### Dynamic CQ

```python
def _determine_cq(self, file: VideoFile) -> int:
    default_cq = self.config.general.cq or 45

    if not file.metadata:
        return default_cq

    # Check for custom CQ from ExifTool
    if file.metadata.custom_cq is not None:
        return file.metadata.custom_cq

    # Check dynamic_cq mapping
    if file.metadata.camera_model:
        for pattern, cq_value in self.config.general.dynamic_cq.items():
            if pattern in file.metadata.camera_model:
                return cq_value

    return default_cq
```

### Auto-Rotation

```python
def _determine_rotation(self, file: VideoFile) -> Optional[int]:
    # Manual rotation overrides all
    if self.config.general.manual_rotation is not None:
        return self.config.general.manual_rotation

    # Check filename patterns
    filename = file.path.name
    for pattern, angle in self.config.autorotate.patterns.items():
        if re.search(pattern, filename):
            return angle

    return None
```

## Graceful Shutdown

```python
# User presses 'S' key
def _on_shutdown_request(self, event: RequestShutdown):
    with self._thread_lock:
        self._shutdown_requested = True
        self._thread_lock.notify_all()  # Wake up all waiting threads

    bus.publish(ActionMessage(message="SHUTDOWN requested"))

# In main loop
while in_flight:
    done, _ = wait(in_flight, timeout=1.0)
    # ...

    # Exit if shutdown and no more in flight
    if self._shutdown_requested and not in_flight:
        logger.info("Shutdown complete")
        break
```

## Refresh Queue

```python
# User presses 'R' key
def _on_refresh_request(self, event: RefreshRequested):
    with self._refresh_lock:
        self._refresh_requested = True

# In main loop
if self._refresh_requested:
    self._refresh_requested = False

    # Re-scan directory
    new_files, new_stats = _perform_discovery(input_dir)

    # Add only new files (not already submitted)
    submitted_paths = {vf.path for vf in in_flight.values()}
    submitted_paths.update(vf.path for vf in pending)

    added = 0
    for vf in new_files:
        if vf.path not in submitted_paths:
            pending.append(vf)
            added += 1

    # Update stats
    bus.publish(DiscoveryFinished(...))
    bus.publish(ActionMessage(message=f"Refreshed: +{added} new files"))
```

## Error Handling

### Corrupted Files

```python
try:
    stream_info = ffprobe_adapter.get_stream_info(video_file.path)
except Exception as e:
    # ffprobe failed - file is corrupted
    err_path.write_text("File is corrupted (ffprobe failed)")
    logger.error(f"Corrupted file: {filename}")
    job.status = JobStatus.FAILED
    bus.publish(JobFailed(job=job, error_message="Corrupted file"))
    return
```

### Hardware Capability

```python
# FFmpegAdapter detects error and sets status
if job.status == JobStatus.HW_CAP_LIMIT:
    # Write .err marker
    err_path.write_text("Hardware is lacking required capabilities")
    # Event already published by FFmpegAdapter
```

### Color Space Fix

```python
input_path, temp_fixed = _check_and_fix_color_space(
    video_file.path,
    output_path,
    stream_info
)

# Use temp_fixed if color space was remuxed
ffmpeg_adapter.compress(job, config, input_path=input_path)

# Cleanup temp file
if temp_fixed and temp_fixed.exists():
    temp_fixed.unlink()
```
