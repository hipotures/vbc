# Pipeline Flow

This document walks through the complete job processing pipeline from discovery to completion.

## Overview

```
Discovery → Queue Management → Job Processing → Post-Processing → Completion
```

## Phase 1: Discovery

### Entry Point

```python
# main.py
orchestrator.run(input_dir)
```

### Steps

1. **Emit start event**
   ```python
   bus.publish(DiscoveryStarted(directory=input_dir))
   ```

2. **Scan directory**
   ```python
   files = list(file_scanner.scan(input_dir))
   # FileScanner yields VideoFile objects
   # Filters: extensions, min_size_bytes, _out directories
   ```

3. **Check existing outputs**
   ```python
   output_dir = input_dir.with_name(f"{input_dir.name}_out")

   for vf in files:
       output_path = output_dir / vf.path.relative_to(input_dir).with_suffix('.mp4')
       err_path = output_path.with_suffix('.err')

       # Skip if output newer than input
       if output_path.exists() and output_path.stat().st_mtime > vf.path.stat().st_mtime:
           already_compressed.add(vf)

       # Skip if .err marker exists (unless clean_errors=True)
       elif err_path.exists() and not config.general.clean_errors:
           ignored_err.add(vf)
   ```

4. **Emit finish event**
   ```python
   bus.publish(DiscoveryFinished(
       files_found=total_files,
       files_to_process=len(files_to_process),
       already_compressed=len(already_compressed),
       ignored_small=ignored_small_count,
       ignored_err=len(ignored_err)
   ))
   ```

### Discovery Stats

| Counter | Description |
|---------|-------------|
| `files_found` | Total matching extensions (including small) |
| `files_to_process` | Files queued for compression |
| `already_compressed` | Output exists and newer than input |
| `ignored_small` | Below `min_size_bytes` |
| `ignored_err` | Has `.err` marker |

## Phase 2: Queue Management

### Submit-on-Demand Pattern

```python
from collections import deque

pending = deque(files_to_process)
in_flight = {}  # future -> VideoFile

def submit_batch():
    max_inflight = prefetch_factor * current_max_threads
    while len(in_flight) < max_inflight and pending:
        vf = pending.popleft()
        future = executor.submit(_process_file, vf, input_dir)
        in_flight[future] = vf
```

### Initial Submission

```python
# Publish lightweight entries before ffprobe fills the first page
bus.publish(QueueUpdated(pending_files=list(pending)))

# Pre-load metadata for first 25 files (for UI queue display)
for vf in list(pending)[:25]:
    vf.metadata = _get_metadata(vf)

# Publish the resolved first page
bus.publish(QueueUpdated(pending_files=list(pending)))

# Submit initial batch
submit_batch()
```

### Main Loop

```python
while in_flight:
    # Wait for at least one job to complete
    done, _ = wait(in_flight, timeout=1.0, return_when=FIRST_COMPLETED)

    for future in done:
        try:
            future.result()  # Raises if job failed
        except Exception as e:
            logger.error(f"Job failed: {e}")

        del in_flight[future]

    # Replenish queue
    submit_batch()
```

## Phase 3: Job Processing

### Thread Slot Acquisition

```python
def _process_file(video_file: VideoFile, input_dir: Path):
    # Block until thread slot available
    with self._thread_lock:
        while self._active_threads >= self._current_max_threads:
            self._thread_lock.wait()  # Sleep until notified

        if self._shutdown_requested:
            return  # Don't start new jobs

        self._active_threads += 1

    try:
        # Process job...
    finally:
        with self._thread_lock:
            self._active_threads -= 1
            self._thread_lock.notify_all()  # Wake waiting threads
```

### Step 1: Pre-checks

```python
# Check for .err marker
err_path = output_path.with_suffix('.err')
if err_path.exists() and not config.general.clean_errors:
    bus.publish(JobFailed(job=..., error_message="Existing error marker"))
    return
```

### Step 2: Stream Info Extraction

```python
try:
    stream_info = ffprobe_adapter.get_stream_info(video_file.path)
except Exception as e:
    # File is corrupted
    err_path.write_text("File is corrupted (ffprobe failed)")
    bus.publish(JobFailed(job=..., error_message="Corrupted file"))
    return
```

### Step 3: Color Space Fix

```python
input_path, temp_fixed = _check_and_fix_color_space(
    video_file.path,
    output_path,
    stream_info
)

# If color_space == "reserved":
# 1. Create temp file with bitstream filter
# 2. Use temp file as input
# 3. Cleanup temp file in finally block
```

### Step 4: Metadata Extraction

```python
# Thread-safe cache lookup
video_file.metadata = _get_metadata(video_file, stream_info)

# _get_metadata() combines:
# - FFprobe stream info (width, height, fps, codec)
# - ExifTool EXIF info (camera model, bitrate, GPS)
```

### Step 5: VBC Encoded Check

```python
# Check if file was already encoded by VBC (to prevent re-encoding)
if metadata.vbc_encoded:
    # Increment skipped_vbc_count
    bus.publish(JobFailed(job=job, error_message="File already encoded by VBC"))
    return
```

### Step 6: Filtering

```python
# Skip AV1
if config.general.skip_av1 and metadata.codec == "av1":
    bus.publish(JobFailed(job=..., error_message="Already AV1"))
    return

# Camera filter
if config.general.filter_cameras:
    cam_model = metadata.camera_model or metadata.camera_raw or ""
    matched = any(pattern in cam_model for pattern in config.general.filter_cameras)
    if not matched:
        bus.publish(JobFailed(job=..., error_message=f"Camera {cam_model} not in filter"))
        return
```

### Step 7: Decision Logic

```python
# Determine quality (dynamic or default)
target_cq = _determine_cq(video_file, use_gpu=config.general.gpu)
# Checks: CLI override → custom_cq from EXIF → dynamic_quality[pattern].cq → default from encoder args

# Determine rotation (manual or pattern-based)
rotation = _determine_rotation(video_file)
# Checks: manual_rotation → autorotate patterns → None
```

### Step 8: Create Job & Start

```python
job = CompressionJob(
    source_file=video_file,
    output_path=output_path,
    rotation_angle=rotation or 0
)

# Emit start event
bus.publish(JobStarted(job=job))
job.status = JobStatus.PROCESSING
```

### Step 9: Compression

```python
# Run FFmpeg
ffmpeg_adapter.compress(
    job=job,
    config=job_config,
    rotate=rotation,
    shutdown_event=self._shutdown_event,
    input_path=input_path  # May be temp_fixed file
)

# FFmpegAdapter:
# 1. Builds ffmpeg command (GPU/CPU, rotation filters, quality)
# 2. Spawns subprocess.Popen
# 3. Monitors stdout for progress
# 4. Detects errors (hw_cap, color errors)
# 5. Sets job.status (COMPLETED/FAILED/HW_CAP_LIMIT/INTERRUPTED)
```

## Phase 4: Post-Processing

### Compression Completed

```python
if job.status == JobStatus.COMPLETED:
    # 1. Copy metadata
    encoder_label = "NVENC AV1 (GPU)" if config.gpu else "SVT-AV1 (CPU)"
    finished_at = datetime.now().isoformat()

    _copy_deep_metadata(
        video_file.path,
        output_path,
        err_path,
        target_cq,
        encoder_label,
        video_file.size_bytes,
        finished_at
    )

    # 2. Check compression ratio
    out_size = output_path.stat().st_size
    in_size = video_file.size_bytes
    ratio = out_size / in_size

    if ratio > (1.0 - config.general.min_compression_ratio):
        # Insufficient savings - keep original
        shutil.copy2(video_file.path, output_path)
        job.error_message = f"Ratio {ratio:.2f} above threshold, kept original"

    # 3. Emit completion event
    bus.publish(JobCompleted(job=job))
```

### Compression Failed

```python
elif job.status in (JobStatus.HW_CAP_LIMIT, JobStatus.FAILED):
    # Write .err marker
    err_path.write_text(job.error_message or "Unknown error")

    # Event already published by FFmpegAdapter
```

### Interrupted

```python
elif job.status == JobStatus.INTERRUPTED:
    # User pressed Ctrl+C
    # Temp files already cleaned by FFmpegAdapter
    bus.publish(JobFailed(job=job, error_message="Interrupted by user"))
```

## Phase 5: Completion

### Main Loop Exit

```python
# Exit when:
# 1. in_flight empty (no active jobs)
# 2. pending empty (no more files to submit)
# OR shutdown_requested (graceful stop)

if not in_flight and not pending:
    # Give UI one more refresh cycle
    time.sleep(1.5)

    if not self._shutdown_requested:
        bus.publish(ProcessingFinished())

    logger.info("All files processed, exiting")
```

### Manifest jobs

An input directory marked `metadata: true` yields one logical `VideoFile` per strict JSON
manifest. Its display path is the requested output, while its identity and directory
ownership remain the manifest path. Discovery reads and validates all JSON files, checks
their input paths, and immediately publishes lightweight queue proxies. The rolling
25-item metadata window then probes every physical part, filters configured audio-only
inputs, and calculates effective aggregate size. Consecutive parts are grouped by
orientation; changes between portrait, landscape, or square start a new output group,
while compatible resolutions inside one group are normalized to its largest frame.
This keeps the UI populated while ffprobe resolves the visible and near-future queue
entries. One cached probe per unchanged part returns stream facts, packet counts, and
packet-timeline duration, so queue refreshes and output validation do not rescan source
video. A request below `min_size_bytes` is a terminal ignored task: VBC creates no output,
keeps all source parts, and moves the unchanged manifest to `_out`.
Packet timelines unwrap the 32-bit millisecond timestamp rollover before calculating
duration. A part whose packet timeline exceeds `metadata.max_duration_seconds` receives
an exceptional decoded-frame count. If `frames / fps` is within the limit, VBC logs the
timestamp anomaly and rebuilds that part's video and audio clocks during compression;
normal parts do not receive this second probe. Preflight rejects the part only when the
decoded-frame duration also exceeds the limit, and still rejects an aggregate effective
duration above the limit before FFmpeg starts.

FFmpeg transcodes each group's effective parts sequentially into complete MP4 containers
whose filenames remain `*.tmp`, then uses the concat demuxer to stream-copy them into the
group output. The first group uses the manifest `output_path`; later groups take the first
available `_1`, `_2`, and subsequent name. Numbered names are shared with preserved
pre-existing MP4 files, and no MP4 is overwritten. This bounds filter-graph memory while
avoiding a second video encode. Only `*.tmp` artifacts are cleaned after success, failure,
or interruption; intermediate files never receive an `.mp4` name. Multipart video uses
timestamp passthrough so FFmpeg does not silently drop closely spaced source frames.
Frame-count verification runs from FFmpeg's encoded-frame statistics and one cached
output packet probe before VBC tags are written, ensuring a tagged output has passed the
frame check. After every group passes verification, `move_after_success` preflights the
configured archive root, capacity, permissions, and every destination before moving
inputs below the producer username directory; a failed preflight behaves as `keep`.
The manifest moves to `_out` only after source policy completion, or to `_err` with a sibling `.err` for any non-interruption
failure. An interrupted request keeps its JSON and reuses already verified group outputs
when it resumes.

### Graceful Shutdown

```python
# User pressed 'S' key
def _on_shutdown_request(self, event):
    with self._thread_lock:
        self._shutdown_requested = True
        self._thread_lock.notify_all()

    bus.publish(ActionMessage(message="SHUTDOWN requested"))

# In main loop:
while in_flight:
    # ... process completions

    if self._shutdown_requested and not in_flight:
        logger.info("Shutdown complete")
        break
```

### Immediate Interrupt

```python
# User pressed Ctrl+C
except KeyboardInterrupt:
    logger.info("Ctrl+C detected - stopping new tasks...")

    # Signal all workers to stop
    self._shutdown_event.set()

    # Stop accepting new tasks
    self._shutdown_requested = True

    # Wait for active FFmpeg processes to exit (max 10s)
    # ...

    # Force shutdown
    executor.shutdown(wait=False, cancel_futures=True)

    raise  # Re-raise to exit with code 130
```

## Concurrency Details

### Condition-Based Thread Control

```python
# Orchestrator stores the thread controller state directly:
# self._thread_lock = threading.Condition()
# self._active_threads = 0
# self._current_max_threads = config.general.threads

# Block until a thread slot is available
with self._thread_lock:
    while self._active_threads >= self._current_max_threads:
        self._thread_lock.wait()

    if self._shutdown_requested:
        return

    self._active_threads += 1

# Process job...

# Release slot
with self._thread_lock:
    self._active_threads -= 1
    self._thread_lock.notify_all()
```

### Dynamic Adjustment

```
State: max_threads=4, active_threads=4

User presses '>'
→ max_threads=5
→ condition.notify_all() wakes waiting threads
→ One waiting worker acquires the new slot (active_threads=5)
→ New job starts immediately

User presses '<'
→ max_threads=3
→ Active threads continue (active_threads=4)
→ When next job finishes, active_threads=3
→ No new jobs start until active_threads < 3
```

## Error Handling

### Corrupted Files

```
ffprobe fails
→ Catch exception
→ Write .err: "File is corrupted"
→ Emit JobFailed
→ Return early
```

### Hardware Capability

```
FFmpeg outputs "Hardware is lacking required capabilities"
→ FFmpegAdapter detects error
→ Set job.status = HW_CAP_LIMIT
→ Emit HardwareCapabilityExceeded
→ Write .err marker
```

### Color Space Issues

```
FFprobe shows color_space=reserved
→ _check_and_fix_color_space()
→ Remux with bitstream filter
→ Use remuxed file as input
→ Cleanup temp file in finally
```

## Performance Optimizations

### Metadata Caching

```python
# Cache to avoid redundant ExifTool calls
_metadata_cache: Dict[Path, VideoMetadata] = {}

def _get_metadata(video_file):
    if video_file.path in _metadata_cache:
        return _metadata_cache[video_file.path]  # Cache hit

    metadata = extract_metadata(video_file)
    _metadata_cache[video_file.path] = metadata
    return metadata
```

**Benefit:** UI queue display doesn't re-extract metadata on every refresh.

### Submit-on-Demand

```python
# OLD: Submit all 1000 files upfront
futures = [executor.submit(process, f) for f in files]
# Memory: 1000 Future objects

# NEW: Submit only prefetch_factor × threads
max_inflight = 1 × 4 = 4 jobs
# Memory: 4 Future objects
```

**Benefit:** Lower memory usage, responsive to thread changes.

### Prefetch Metadata for Queue

```python
# Pre-load metadata for next 25 files in queue
for vf in list(pending)[:25]:
    if not vf.metadata:
        vf.metadata = _get_metadata(vf)

# UI displays camera model without delay
```

**Benefit:** Queue panel shows camera info immediately.

## Next Steps

- [Event System](events.md) - Event types and flow
- [Architecture Overview](overview.md) - High-level design
- [API Reference](../api/pipeline.md) - Orchestrator API
