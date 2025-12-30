# Runtime Controls

VBC provides interactive keyboard controls to adjust behavior while compression is running.

## Keyboard Reference

| Key | Action | Description |
|-----|--------|-------------|
| `<` or `,` | Decrease threads | Reduce max concurrent threads by 1 (min: 1) |
| `>` or `.` | Increase threads | Increase max concurrent threads by 1 (max: 16) |
| `S` or `s` | Graceful shutdown / Cancel | Stop accepting new jobs, finish active compressions. Press again to cancel shutdown |
| `R` or `r` | Refresh queue | Re-scan input directory and add new files |
| `C` or `c` | Toggle config | Show/hide configuration overlay |
| `Esc` | Hide config | Close configuration overlay |
| `Ctrl+C` | Immediate interrupt | Terminate active compressions immediately |

## Thread Adjustment

### Decrease Threads (`,` or `<`)

Reduces the maximum concurrent threads by 1.

**Behavior:**
- Minimum: 1 thread
- Currently active jobs continue
- No new jobs start until active count drops below new limit
- UI feedback: "Threads: 8 → 7"

**Use case:** Reduce system load if computer becomes sluggish

**Example:**
```
# Running with 8 threads, CPU at 100%
Press: ,
Result: Threads: 8 → 7
# Active jobs continue, new limit is 7
```

### Increase Threads (`.` or `>`)

Increases the maximum concurrent threads by 1.

**Behavior:**
- Maximum: 16 threads (NVENC session limit)
- New slots available immediately
- Queued jobs start filling new slots
- UI feedback: "Threads: 4 → 5"

**Use case:** Speed up compression if system can handle more load

**Example:**
```
# Running with 4 threads, CPU at 50%
Press: .
Result: Threads: 4 → 5
# New job starts immediately if queue not empty
```

## Shutdown Control

### Graceful Shutdown (`S`)

Stops accepting new compression jobs while allowing active jobs to finish. **Press `S` again to cancel shutdown.**

**Behavior:**
1. Sets shutdown flag
2. No new jobs start (queue stops advancing)
3. Active compressions continue normally
4. UI shows "SHUTDOWN requested (press S to cancel)"
5. Exit when all active jobs complete
6. **Press `S` again to cancel shutdown and resume normal operation**

**Use case:** Need to stop processing but don't want to lose progress on active files

**Timeline (with cancellation):**
```
t=0s  : Press 'S'
      → UI: "SHUTDOWN requested (press S to cancel)"
      → Active: 4 jobs (continue)
      → Queue: 50 jobs (frozen)

t=10s : Press 'S' again (cancel shutdown)
      → UI: "SHUTDOWN cancelled"
      → Active: 4 jobs (continue)
      → Queue: 50 jobs (resumes)
      → New jobs start as slots open

t=30s : Queue processing continues normally
```

**Timeline (without cancellation):**
```
t=0s  : Press 'S'
      → UI: "SHUTDOWN requested (press S to cancel)"
      → Active: 4 jobs (continue)
      → Queue: 50 jobs (frozen)

t=30s : 2 jobs finish
      → Active: 2 jobs (continue)
      → Queue: 50 jobs (still frozen)

t=45s : Last 2 jobs finish
      → Active: 0 jobs
      → Program exits
```

### Immediate Interrupt (Ctrl+C)

Terminates active FFmpeg processes immediately.

**Behavior:**
1. Signal all active FFmpeg processes to terminate
2. Wait up to 10 seconds for processes to exit
3. Clean up `.tmp` files
4. Exit with code 130
5. Active jobs marked as `INTERRUPTED`

**Use case:** Emergency stop (system overheating, urgent restart needed)

**Warning:** Active compressions are lost (need to re-process)

**Timeline:**
```
t=0s  : Press Ctrl+C
      → UI: "Ctrl+C - interrupting active compressions..."
      → Signal all ffmpeg processes: SIGTERM

t=2s  : FFmpeg processes exit
      → Clean up .tmp files
      → Program exits (code 130)
```

## Queue Refresh (`R`)

Re-scans the input directory and adds newly discovered files to the queue.

**Behavior:**
1. Scans input directory (same as initial discovery)
2. Compares with already-submitted files (in-flight + pending + completed)
3. Adds new files to end of queue
4. Updates discovery counters
5. UI feedback: "Refreshed: +5 new files" or "Refreshed: no changes"

**Use case:** New videos added to directory while VBC is running

**Example:**
```
# VBC running, processing 10 files
# User copies 3 new videos to input directory
Press: R
Result: "Refreshed: +3 new files"
# Queue now has 3 more files to process
```

**Note:** Files already completed or in queue are not duplicated.

## Configuration Overlay (`C`)

Toggles full configuration display.

**Behavior:**
- Shows full VBC configuration (similar to startup)
- Overlays main dashboard
- Press `C` again or `Esc` to close

**Use case:** Verify settings without restarting

**Display:**
```
┌─ CONFIGURATION ────────────────────────────────┐
│ Video Batch Compression - NVENC AV1 (GPU)     │
│ Start: 2025-12-21 15:30:00                    │
│ Input: /videos                                │
│ Output: /videos_out                           │
│ Threads: 8 (Prefetch: 1x)                    │
│ Encoder: NVENC AV1 | Preset: p7 (Slow/HQ)    │
│ Quality: CQ45 (Global Default)                │
│ Dynamic CQ: ILCE-7RM5:38, DC-GH7:40           │
│ Camera Filter: None                           │
│ ... (full config)                             │
│                                               │
│ Press Esc to close                            │
└────────────────────────────────────────────────┘
```

## Concurrency Behavior

### How Thread Control Works

VBC uses a **ThreadController** pattern with condition variables:

```python
# Worker thread blocks until slot available
with thread_lock:
    while active_threads >= max_threads:
        thread_lock.wait()  # Sleep until notified

    if shutdown_requested:
        return  # Don't start new jobs

    active_threads += 1

# Process job...

with thread_lock:
    active_threads -= 1
    thread_lock.notify_all()  # Wake up waiting threads
```

**Benefits:**
- No polling (efficient CPU usage)
- Instant response to keyboard changes
- Thread-safe (no race conditions)

### Submit-on-Demand Queue

VBC maintains a dynamic queue:

```python
max_inflight = prefetch_factor × current_max_threads

# Example: prefetch_factor=1, threads=4
max_inflight = 1 × 4 = 4 jobs in queue

# User presses '>' (threads: 4 → 5)
max_inflight = 1 × 5 = 5 jobs in queue
# New job submitted immediately
```

## Best Practices

### Performance Tuning

1. **Start conservative**: Begin with 4 threads
2. **Monitor CPU**: Use system monitor (htop, Task Manager)
3. **Adjust up**: Press `.` if CPU < 80%
4. **Adjust down**: Press `,` if system becomes unresponsive

### NVENC Session Limits

NVIDIA GPUs have encoding session limits that vary by generation:

- **RTX 30-series** (GeForce): ~5 concurrent sessions
- **RTX 40-series** (e.g., RTX 4090): 10-12 concurrent sessions
- **Professional GPUs** (Quadro, A-series): Higher limits (often unlimited)

**Recommendation:**
- RTX 30-series: Max 4-5 threads for GPU mode
- RTX 40-series: Max 10-12 threads for GPU mode
- Professional GPUs: Max 8-16 threads

If you exceed the limit, you'll see `HW_CAP` errors in the UI.

### CPU Mode Threading

For CPU encoding (SVT-AV1):

- **High-end CPU** (16+ cores): 8-12 threads
- **Mid-range CPU** (8-12 cores): 4-6 threads
- **Low-end CPU** (4-6 cores): 2-3 threads

SVT-AV1 uses multiple threads per encode, so total system threads = `vbc_threads × svt_threads_per_encode`.

### Graceful vs Immediate Shutdown

| Scenario | Use | Reason |
|----------|-----|--------|
| Normal completion | `S` (Graceful) | Finish active jobs, no waste |
| System maintenance soon | `S` (Graceful) | Controlled stop |
| Accidental shutdown | `S` twice (Cancel) | Resume processing after mistaken press |
| Urgent restart | `Ctrl+C` | Immediate termination |
| Testing/debugging | `Ctrl+C` | Fast iteration |

### Refresh Queue

**When to use:**
- Continuous monitoring scenario (e.g., incoming drone footage)
- Long-running batch with additions mid-process
- Shared directory with multiple users

**When NOT to use:**
- One-time batch (all files known upfront)
- Performance-critical runs (refresh has small overhead)

## UI Feedback

All keyboard actions show feedback in the **Progress panel**:

```
┌────────────────────────────────────────────────┐
│ Progress: 42/100 (42%) | Active threads: 8    │
│ Last action: Threads: 7 → 8                   │
└────────────────────────────────────────────────┘
```

Feedback expires after 60 seconds.

## Troubleshooting

### Threads Not Increasing

**Symptom:** Press `.` but threads stay the same

**Causes:**
1. Already at max (16 threads)
2. Shutdown requested (no new jobs allowed)

**Solution:** Check UI for "SHUTDOWN requested" message

### Jobs Not Starting

**Symptom:** Active threads < max threads, but queue frozen

**Causes:**
1. Shutdown requested (`S` pressed)
2. All remaining files skipped (AV1/camera filter)

**Solution:**
- Check for "SHUTDOWN requested" message in UI
- Press `S` again to cancel shutdown and resume
- Check summary counters for skipped files
- Press `R` to refresh queue

### Accidental Shutdown

**Symptom:** Pressed `S` by accident, want to continue processing

**Solution:**
- Press `S` again immediately to cancel
- UI will show "SHUTDOWN cancelled"
- Queue resumes normal operation
- New jobs start as slots become available

**Note:** You can cancel shutdown at any time before all active jobs finish.

### Refresh Not Finding Files

**Symptom:** Press `R`, but "Refreshed: no changes"

**Causes:**
1. No new files in directory
2. New files below `min_size_bytes`
3. New files already processed

**Solution:**
- Verify files exist with `ls -lh /input/directory`
- Check `min_size_bytes` in config

## Next Steps

- [Advanced Features](advanced.md) - Dynamic CQ, auto-rotation
- [CLI Reference](cli.md) - Command-line arguments
- [Architecture](../architecture/overview.md) - How controls work internally
