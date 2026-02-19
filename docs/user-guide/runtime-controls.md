# Runtime Controls

VBC provides interactive keyboard controls to adjust behavior while compression is running.

## Keyboard Reference

| Key | Action | Description |
|-----|--------|-------------|
| `<` or `,` | Decrease threads | Reduce max concurrent threads by 1 (min: 1) |
| `>` or `.` | Increase threads | Increase max concurrent threads by 1 (interactive max: 8) |
| `S` or `s` | Graceful shutdown / Cancel | Stop accepting new jobs, finish active compressions. Press again to cancel shutdown |
| `R` or `r` | Refresh queue | Re-scan input directory and add new files |
| `C` or `c` | Overlay: Prefs | Toggle Prefs tab |
| `F` or `f` | Overlay: I/O | Toggle I/O tab |
| `E` or `e` | Overlay: Ref | Toggle Ref tab |
| `L` or `l` | Overlay: Logs | Toggle logs tab (session errors) |
| `M` or `m` | Overlay: Keys | Toggle Keys tab |
| `T` or `t` | Overlay: TUI | Toggle TUI tab |
| `Tab` | Cycle overlay tabs | Next tab |
| `[` | Logs previous page | Previous page in Logs tab |
| `]` | Logs next page | Next page in Logs tab |
| `D` or `d` | Dim overlay | Cycle overlay dim level |
| `G` or `g` | Rotate GPU metric | Cycle GPU sparkline metric |
| `W` or `w` | Sparkline preset | Cycle preset |
| `P` or `p` | Sparkline palette | Cycle palette |
| `Esc` | Close overlay | Close active overlay tab |
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
- Interactive maximum: 8 threads
- New slots available immediately
- Queued jobs start filling new slots
- UI feedback: "Threads: 4 → 5"

Startup threads from CLI/config are validated as `>0` and can be higher; practical upper bound is executor `max_workers=16`.

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

## Wait Mode

When `wait_on_finish: true` (or `--wait` CLI flag) is set, VBC does not exit automatically after all tasks complete. Instead, it shows **WAITING** status in the top bar and waits for user input.

**Behavior:**

- After all files are processed, the status bar changes to **WAITING** with the hint line `R = restart scan  │  S / Ctrl+C = exit`
- Press **R** to restart the scan (full re-discovery and processing of any new files)
- Press **S** or **Ctrl+C** to exit VBC

**Use case:** Run VBC repeatedly without restarting the application — for example, processing batches as files arrive.

**Example:**
```bash
uv run vbc /videos --wait --bell
```

```
# VBC finishes processing 20 files
# Status changes to WAITING, terminal bell rings (--bell)
# User copies more files, presses R
# VBC re-scans, finds 5 new files, processes them
# Status changes to WAITING again
# User presses S → VBC exits cleanly
```

**Bell integration:**

When `bell_on_finish: true` (or `--bell`), a terminal bell plays when VBC enters the wait state. This lets you know processing is done without watching the screen.

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
│ Dynamic Quality: ILCE-7RM5:38, DC-GH7:40           │
│ Camera Filter: None                           │
│ ... (full config)                             │
│                                               │
│ Press Esc to close                            │
└────────────────────────────────────────────────┘
```

## Session Error Logs (`L`)

Opens the `Logs` tab in overlay and shows errors captured since app start (current session only).

**Behavior:**
1. One error entry uses exactly 2 lines:
   - Line 1: source file + best-effort metadata (size, resolution/FPS/codec when available)
   - Line 2: error message in muted red (single-line, cropped if too long)
2. Default pagination is 10 entries per page (max 20 lines of log content).
3. If there are more than 10 errors, use `[` and `]` to move between pages.
4. The newest errors appear first.

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
- RTX 40-series: Max 8 threads in VBC (hardware often supports 10-12 sessions)
- Professional GPUs: Max 8 threads (VBC runtime limit)

If total concurrent GPU sessions (VBC + other processes) exceed hardware limits, you'll see `HW_CAP` errors in the UI.

### CPU Mode Threading

For CPU encoding (SVT-AV1):

- **High-end CPU** (16+ cores): 6-8 threads
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
1. Already at max (8 threads)
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

- [Advanced Features](advanced.md) - Dynamic Quality, auto-rotation
- [CLI Reference](cli.md) - Command-line arguments
- [Architecture](../architecture/overview.md) - How controls work internally
