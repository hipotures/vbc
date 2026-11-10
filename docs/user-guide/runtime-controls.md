# Runtime Controls

The dashboard (`vbc tui`, and `vbc run`, which starts a private server with the same dashboard) provides keyboard controls while compression is running. The keys act on the server; the only difference is quitting: `vbc tui` leaves its server running, `vbc run` stops its private server.

## Keyboard Reference

| Key | Action | Description |
|-----|--------|-------------|
| `<` or `,` | Decrease threads | Reduce max concurrent threads by 1 (min: 1) |
| `>` or `.` | Increase threads | Increase max concurrent threads by 1 (max: `general.max_threads`, default 8) |
| `S` or `s` | Graceful stop / Cancel | Stop accepting new jobs, finish active compressions. Press again while stopping to cancel. Processing then stays `STOPPED` (`R` resumes) |
| `R` or `r` | Refresh queue | Re-scan input directories and add new files (also resumes after `STOPPED` or `ERROR`) |
| `P` or `p` | Overlay: Prefs | Toggle Prefs tab |
| `F` or `f` | Overlay: I/O | Toggle I/O tab |
| `D` or `d` | Overlay: Dirs | Toggle Dirs tab |
| `E` or `e` | Overlay: Ref | Toggle Ref tab (status codes, result symbols, `G`/`L` config source icons) |
| `L` or `l` | Overlay: Logs | Toggle logs tab (session errors) |
| `K` or `M` | Overlay: Keys (menu) | Open the menu on the Keys tab |
| `T` or `t` | Overlay: TUI | Toggle TUI tab |
| `Tab` | Cycle overlay tabs | Next tab: Keys -> Prefs -> I/O -> Dirs -> TUI -> Ref -> Logs |
| `[` | Logs previous page | Previous page in Logs tab |
| `]` | Logs next page | Next page in Logs tab |
| `I` or `i` | Dim overlay | Cycle overlay dim level |
| `G` or `g` | Rotate metric | Cycle GPU/CPU sparkline metric |
| `W` or `w` | Sparkline preset | Cycle preset |
| `O` or `o` | Sparkline palette | Cycle palette |
| `Shift+U` | Unlock (`vbc tui`) | Unlock keyboard after auto-lock (auto-locks after 60 s of inactivity, configurable via `VBC_TUI_LOCK_AFTER_S`) |
| `Esc` | Close overlay | Close active overlay tab |
| `Q` / `Ctrl+Q` / `Ctrl+C` | Quit | `vbc tui`: quit the client at once, the server keeps running. `vbc run`: quit and stop the private server (running jobs are interrupted) |

## Dirs Tab Controls

When the Dirs tab is active, extra directory-management shortcuts are available:

| Key | Action | Description |
|-----|--------|-------------|
| `↑` / `↓` | Move cursor | Select previous or next directory row |
| `Shift+↑` / `Shift+↓` | Reorder selected row | Swap the selected directory with the previous or next row |
| `Space` | Toggle selected | Enable or disable the selected directory |
| `A` or `a` | Add directory | Enter add-directory input mode (bracketed paste types into the path buffer) |
| `R` or `r` | Scan mounts (`vbc tui`) | Ask the server to scan media mounts and add new folders as disabled entries |
| `Delete` / `Backspace` | Mark delete | Mark the selected directory for deletion |
| `S` or `s` | Apply changes | Save pending Dirs changes; shows a message if there are none |
| `Esc` | Cancel input | Exit add-directory input mode without applying typed text |

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

## Shutdown / Stop Control

### Graceful Stop / Shutdown (`S`)

Stops accepting new compression jobs while allowing active jobs to finish. **Press `S` again while stopping to cancel.**

**Behavior:**
1. Sets shutdown/stop flag
2. No new jobs start (queue stops advancing)
3. Active compressions continue normally
4. **Press `S` again while stopping to cancel and resume normal operation**
5. When all active jobs complete:
   processing enters `STOPPED` while the server process stays running; press
   `R` (or run `vbc refresh`) to resume, or quit the dashboard (in `vbc run`
   this also ends the private server).

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
      → Status: STOPPED (R resumes, Q quits)
```

### Immediate Interrupt (`vbc stop --interrupt`, quitting `vbc run`)

Terminates active FFmpeg processes immediately. Triggered by
`vbc stop --interrupt` (the server keeps running), by a second `SIGINT` to
`vbc serve`, or by quitting `vbc run` while jobs are running (its private
server then ends). In `vbc tui`, `Q`, `Ctrl+Q` and `Ctrl+C` only quit the
client.

**Behavior:**
1. Signal all active FFmpeg processes to terminate
2. Clean up `.tmp` files (no `.err` markers are written)
3. Active jobs are marked `INTERRUPTED`; their sources stay in place and are
   compressed again by the next pass

**Use case:** Emergency stop (system overheating, urgent restart needed)

**Warning:** Work on the active files is lost (they are re-processed later)

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

## Waiting After a Pass

The server never exits when the queue is empty. If current-session failures
are repairable and `auto_repair_errors: true`, it first shows **REPAIR**,
attempts one repair pass and compresses repaired files. It then shows
**WAITING** in the top bar.

**Behavior:**

- Press **R** (or run `vbc refresh`) to rescan (full re-discovery and
  processing of any new files).
- Directories with `idle_interval` are re-scanned automatically while WAITING.
- Metadata directories configured with `watch: true` use Linux inotify to request
  incremental discovery of the exact JSON paths reported after completed writes or
  atomic moves. Events are coalesced for one second, work while ACTIVE or WAITING,
  and do not interrupt running jobs or rescan existing manifests. Queue overflow
  falls back to a full refresh.
- Regular video directories configured with `watch: true` and
  `watch_mode: polling` are periodically scanned for new paths matching
  `general.extensions`. `poll_interval_seconds` sets the period per directory
  (default `1.0`). A match requests the same full refresh as **R**, so this mode
  works on NFS. Files with temporary extensions are ignored until renamed to a
  configured final extension.
- Metadata directories may also use `watch_mode: polling` when inotify events are
  unavailable, for example over NFS. Polling detects final `*.json` names and
  ignores temporary names before requesting a full refresh.
- With `vbc run`, quit the dashboard (**Q**) when you are done; this also ends
  its private server.

**Use case:** Process batches as files arrive without restarting anything.

## Configuration Overlay (`P`)

Toggles full configuration display.

**Behavior:**
- Shows full VBC configuration (similar to startup)
- Overlays main dashboard
- Press `P` again or `Esc` to close

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
1. One error entry uses 2 lines:
   - Line 1: source file + best-effort metadata (size, resolution/FPS/codec, duration when available) + error timestamp (`YYYY-MM-DD HH:MM:SS`)
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
