"""Read-only HTMX web dashboard for VBC.

Serves a single-page dashboard that auto-refreshes via HTMX polling every 2s.
Runs as a daemon thread — stops automatically when VBC exits.

No new dependencies: uses stdlib http.server + socketserver only.
HTMX 2.0.8 loaded from jsDelivr CDN.
"""
from __future__ import annotations

import html
import logging
import re
import socketserver
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from vbc.ui.state import UIState

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765

# ---------------------------------------------------------------------------
# Embedded HTML page (never written to disk)
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VBC Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/htmx.org@2.0.8/dist/htmx.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:          #0d1117;
    --bg-panel:    #161b22;
    --border:      #30363d;
    --text:        #e6edf3;
    --dim:         #7d8590;
    --accent:      #58a6ff;
    --green:       #3fb950;
    --yellow:      #d29922;
    --red:         #f85149;
    --cyan:        #39d0d8;
    --bar-track:   #30363d;
    --bar-global:  #1f6feb;
    --bar-job:     #da3633;
    --font: 'JetBrains Mono', 'Fira Mono', 'Cascadia Code', Consolas, monospace;
  }

  html, body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
    font-size: 13px;
    line-height: 1.5;
    min-height: 100vh;
  }

  /* === Layout === */
  .dashboard {
    max-width: 1600px;
    margin: 0 auto;
    padding: 10px;
    display: grid;
    gap: 8px;
    grid-template-columns: 1fr;
  }

  @media (min-width: 1100px) {
    .dashboard {
      grid-template-columns: 58fr 42fr;
      grid-template-areas:
        "header   header"
        "progress progress"
        "active   activity"
        "active   queue";
    }
    .slot-header   { grid-area: header; }
    .slot-progress { grid-area: progress; }
    .slot-active   { grid-area: active; }
    .slot-activity { grid-area: activity; }
    .slot-queue    { grid-area: queue; }
  }

  /* === Panel base === */
  .panel {
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px 16px;
  }

  .panel-title {
    font-size: 11px;
    font-weight: 700;
    color: var(--cyan);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 10px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
  }

  /* === Typography helpers === */
  .dim      { color: var(--dim); }
  .accent   { color: var(--accent); }
  .ok       { color: var(--green); }
  .warn     { color: var(--yellow); }
  .fail     { color: var(--red); }
  .sep      { color: var(--dim); margin: 0 2px; }
  .empty    { color: var(--dim); font-style: italic; padding: 4px 0; }

  /* === Header === */
  .hdr-body  { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }
  .hdr-left  { flex: 1; min-width: 0; }
  .status-row { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
  .kpi-row   { margin-bottom: 3px; }
  .hint      { font-size: 11px; color: var(--dim); }

  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .dot-green  { background: var(--green); }
  .dot-yellow { background: var(--yellow); animation: blink 1s infinite; }
  .dot-red    { background: var(--red); }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }

  .badge { font-weight: 700; font-size: 13px; }
  .badge-active      { color: var(--cyan); }
  .badge-finished    { color: var(--green); }
  .badge-shutdown    { color: var(--yellow); }
  .badge-interrupted { color: var(--red); }

  /* GPU panel */
  .gpu-panel   { border-left: 2px solid var(--border); padding-left: 14px; min-width: 260px; flex-shrink: 0; }
  .gpu-name    { font-size: 11px; color: var(--dim); margin-bottom: 4px; }
  .gpu-metrics { display: flex; flex-wrap: wrap; gap: 4px 6px; margin-bottom: 6px; font-size: 12px; }
  .gpu-bar-row { display: flex; align-items: center; gap: 6px; }
  .gpu-bar-lbl { font-size: 10px; color: var(--dim); }
  .gpu-green { color: var(--green); }
  .gpu-yellow { color: var(--yellow); }
  .gpu-red   { color: var(--red); }

  /* Mobile: stack GPU below status */
  @media (max-width: 700px) {
    .hdr-body { flex-direction: column; gap: 10px; }
    .gpu-panel {
      border-left: none;
      border-top: 1px solid var(--border);
      padding-left: 0;
      padding-top: 10px;
      min-width: 0;
      width: 100%;
    }
    .gpu-metrics { font-size: 11px; gap: 2px 4px; flex-wrap: nowrap; }
    .gpu-metrics .sep { margin: 0 1px; }
  }

  /* === Progress bars === */
  .bar-wrap { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .bar-track {
    flex: 1;
    height: 8px;
    background: var(--bar-track);
    border-radius: 4px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.6s ease;
  }
  .bar-fill-global { background: var(--bar-global); }
  .bar-fill-job    { background: var(--bar-job); }
  .bar-pct { font-weight: 700; min-width: 52px; text-align: right; font-size: 13px; }
  .bar-meta { font-size: 12px; color: var(--dim); }

  /* Mini bars for GPU */
  .mini-track {
    flex: 1;
    height: 5px;
    background: var(--bar-track);
    border-radius: 3px;
    overflow: hidden;
    min-width: 50px;
  }
  .mini-fill { height: 100%; border-radius: 3px; transition: width 0.8s ease; }
  .mini-fill.gpu-green  { background: var(--green); }
  .mini-fill.gpu-yellow { background: var(--yellow); }
  .mini-fill.gpu-red    { background: var(--red); }

  /* === Active jobs === */
  .job-row { margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px solid var(--border); }
  .job-row:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
  .job-name-row { display: flex; align-items: baseline; gap: 6px; margin-bottom: 3px; flex-wrap: wrap; }
  .job-dot  { color: var(--green); font-size: 11px; flex-shrink: 0; }
  .job-name { font-weight: 600; word-break: break-all; }
  .job-meta { font-size: 11px; color: var(--dim); }
  .job-bar-row { display: flex; align-items: center; gap: 6px; }
  .job-bar  { height: 6px; }
  .job-pct  { font-size: 12px; min-width: 50px; text-align: right; }
  .job-eta  { font-size: 11px; color: var(--dim); }

  /* === Activity feed === */
  .act-row  { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 8px; }
  .act-row:last-child { margin-bottom: 0; }
  .act-icon { font-size: 14px; flex-shrink: 0; margin-top: 1px; }
  .act-body { flex: 1; min-width: 0; }
  .act-name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .act-stat { font-size: 11px; }

  /* === Queue === */
  .q-item { margin-bottom: 5px; }
  .q-item:last-of-type { margin-bottom: 0; }
  .q-name-row { display: flex; align-items: baseline; gap: 4px; overflow: hidden; }
  .q-arrow { flex-shrink: 0; color: var(--dim); }
  .q-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .q-meta { padding-left: 14px; font-size: 11px; color: var(--dim); }
  .q-more { margin-top: 6px; font-size: 11px; color: var(--dim); }

  /* === Progress text in header === */
  .prog-header { margin-bottom: 8px; }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>
<div class="dashboard">

  <!-- Header -->
  <div class="slot-header"
       hx-get="/api/header"
       hx-trigger="load, every 2s"
       hx-swap="outerHTML">
    <div class="panel"><div class="panel-title">VBC</div><div class="dim">Connecting&hellip;</div></div>
  </div>

  <!-- Progress -->
  <div class="slot-progress"
       hx-get="/api/progress"
       hx-trigger="load, every 2s"
       hx-swap="outerHTML">
    <div class="panel"><div class="panel-title">PROGRESS</div><div class="dim">Loading&hellip;</div></div>
  </div>

  <!-- Active Jobs -->
  <div class="slot-active"
       hx-get="/api/active"
       hx-trigger="load, every 2s"
       hx-swap="outerHTML">
    <div class="panel"><div class="panel-title">ACTIVE JOBS</div><div class="dim">Loading&hellip;</div></div>
  </div>

  <!-- Activity Feed -->
  <div class="slot-activity"
       hx-get="/api/activity"
       hx-trigger="load, every 2s"
       hx-swap="outerHTML">
    <div class="panel"><div class="panel-title">ACTIVITY FEED</div><div class="dim">Loading&hellip;</div></div>
  </div>

  <!-- Queue -->
  <div class="slot-queue"
       hx-get="/api/queue"
       hx-trigger="load, every 2s"
       hx-swap="outerHTML">
    <div class="panel"><div class="panel-title">QUEUE</div><div class="dim">Loading&hellip;</div></div>
  </div>

</div>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Format helpers (pure functions, mirror dashboard.py conventions)
# ---------------------------------------------------------------------------

def _esc(text: object) -> str:
    """HTML-escape a value. Always call this on data from domain models."""
    return html.escape(str(text), quote=True)


_SPIN_NORMAL = "●○◉◎"
_SPIN_ROTATE = "◐◓◑◒"
_SPIN_CUSTOM = "◍◌"
def _spinner(filename: str, rotation: int, custom_cq) -> str:
    """Return current spinner character for a job — one frame per 2s HTMX poll."""
    frame = int(time.time() / 2)
    h = hash(filename) & 0xFFFFFF
    if rotation:
        chars = _SPIN_ROTATE
    elif custom_cq is not None:
        chars = _SPIN_CUSTOM
    else:
        chars = _SPIN_NORMAL
    return chars[(frame + h) % len(chars)]


def _fmt_size(size_bytes: Optional[int]) -> str:
    """Format bytes to human-readable string: 0B, 1.2KB, 45.1MB, 3.2GB."""
    if not size_bytes:
        return "0B"
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(size_bytes)
    idx = 0
    while val >= 1024.0 and idx < len(units) - 1:
        val /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(val)}B"
    return f"{val:.1f}{units[idx]}"


def _fmt_time(seconds: Optional[float]) -> str:
    """Format seconds: 59s, 01m 01s, 1h 01m."""
    if seconds is None or seconds < 0:
        return "--:--"
    s = float(seconds)
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60):02d}m {int(s % 60):02d}s"
    return f"{int(s // 3600)}h {int((s % 3600) // 60):02d}m"


def _fmt_fps(metadata: object) -> str:
    """Extract fps string from VideoMetadata, or empty string."""
    if metadata and getattr(metadata, "fps", None):
        return f"{int(metadata.fps)}fps"
    return ""


def _parse_gpu_num(s: object) -> float:
    """Parse numeric value from GPU strings like '52C', '30%', '112W'."""
    if not s:
        return 0.0
    m = re.search(r"(\d+\.?\d*)", str(s))
    return float(m.group(1)) if m else 0.0


def _gpu_cls(val: float, norm: float, high: float) -> str:
    """Return CSS color class for a GPU metric."""
    if val < norm:
        return "gpu-green"
    if val > high:
        return "gpu-red"
    return "gpu-yellow"


def _quality_str(job: object) -> str:
    """Extract human-readable quality string from a CompressionJob."""
    qd = getattr(job, "quality_display", None)
    if qd:
        qd = str(qd).strip()
        mbps = re.fullmatch(r"(\d+(?:\.\d+)?)\s*Mbps", qd, flags=re.IGNORECASE)
        if mbps:
            return f"{int(round(float(mbps.group(1))))}Mbps"
        return qd
    qv = getattr(job, "quality_value", None)
    if qv is not None:
        return f"cq{qv}"
    return ""


# ---------------------------------------------------------------------------
# Stats computation (one lock acquisition, returns plain Python types)
# ---------------------------------------------------------------------------

def _compute_stats(state: "UIState") -> dict:
    """Read all needed data from UIState in a single lock acquisition.

    All returned values are plain Python types (str, int, float, list, bool)
    so renderers can work without touching the lock.
    """
    with state._lock:
        now = datetime.now()

        # Counters
        completed = state.completed_count
        failed = state.failed_count
        files_to_process = state.files_to_process

        # Status
        is_finished = state.finished
        is_interrupted = state.interrupt_requested
        is_shutdown = state.shutdown_requested

        # Job snapshots (shallow copies of references)
        active_jobs = list(state.active_jobs)
        recent_jobs = list(state.recent_jobs)
        pending_files = list(state.pending_files)
        job_start_times = dict(state.job_start_times)

        # GPU
        gpu_data = dict(state.gpu_data) if state.gpu_data else None

        # Bytes
        total_in = state.total_input_bytes
        total_out = state.total_output_bytes

        # Timing
        elapsed = 0.0
        if state.processing_start_time:
            elapsed = (now - state.processing_start_time).total_seconds()

        # Throughput — 30s sliding window (mirrors dashboard.py)
        window_sec = 30.0
        cutoff = now.timestamp() - window_sec
        bytes_window = 0
        files_window = 0
        for ts, size in reversed(list(state.throughput_history)):
            if ts.timestamp() < cutoff:
                break
            bytes_window += size
            files_window += 1

        time_window = min(elapsed, window_sec)
        throughput_bps = 0.0
        if time_window > 0.1 and bytes_window > 0:
            throughput_bps = bytes_window / time_window
        elif elapsed > 0 and total_in > 0:
            throughput_bps = total_in / elapsed

        # ETA
        done_since = (
            (completed - state.completed_count_at_last_discovery)
            + (failed - state.failed_count_at_last_discovery)
        )
        rem = max(0, files_to_process - done_since)
        eta_seconds: Optional[float] = None
        if files_window > 0 and time_window > 0 and rem > 0:
            eta_seconds = (time_window / files_window) * rem

        # Global progress % by bytes
        pending_bytes = sum(getattr(f, "size_bytes", 0) for f in pending_files)
        active_bytes = sum(getattr(j.source_file, "size_bytes", 0) for j in active_jobs)
        total_size = pending_bytes + active_bytes + total_in
        pct_global = (total_in / total_size * 100.0) if total_size > 0 else 0.0

        space_saved = max(0, total_in - total_out)
        ratio = (total_out / total_in) if total_in > 0 else 0.0
        active_count = len(active_jobs)
        target_threads = 0 if is_shutdown else state.current_threads
        source_folders = state.source_folders_count

    return {
        "now": now,
        "completed": completed,
        "failed": failed,
        "files_to_process": files_to_process,
        "is_finished": is_finished,
        "is_interrupted": is_interrupted,
        "is_shutdown": is_shutdown,
        "active_jobs": active_jobs,
        "recent_jobs": recent_jobs,
        "pending_files": pending_files,
        "job_start_times": job_start_times,
        "gpu_data": gpu_data,
        "total_in": total_in,
        "total_out": total_out,
        "total_size": total_size,
        "elapsed": elapsed,
        "throughput_bps": throughput_bps,
        "eta_seconds": eta_seconds,
        "pct_global": pct_global,
        "space_saved": space_saved,
        "ratio": ratio,
        "active_count": active_count,
        "target_threads": target_threads,
        "source_folders": source_folders,
        "files_window": files_window,
    }


# ---------------------------------------------------------------------------
# HTML fragment renderers
# ---------------------------------------------------------------------------

def _render_header(s: dict) -> str:
    if s["is_finished"]:
        dot_cls, badge_cls, label = "dot-green", "badge-finished", "FINISHED"
    elif s["is_interrupted"]:
        dot_cls, badge_cls, label = "dot-red", "badge-interrupted", "INTERRUPTED"
    elif s["is_shutdown"]:
        dot_cls, badge_cls, label = "dot-yellow", "badge-shutdown", "SHUTTING DOWN"
    else:
        dot_cls, badge_cls, label = "dot-green", "badge-active", "ACTIVE"

    a, t = s["active_count"], s["target_threads"]
    threads_disp = str(a) if a == t else f"{a} &rarr; {t}"

    tp_str = f"{s['throughput_bps'] / 1_048_576:.1f} MB/s"
    eta_str = _fmt_time(s["eta_seconds"])
    saved_str = _esc(_fmt_size(s["space_saved"]))
    ratio_pct = (1.0 - s["ratio"]) * 100.0

    # GPU block
    gpu_html = ""
    g = s["gpu_data"]
    if g:
        t_val = _parse_gpu_num(g.get("temp"))
        f_val = _parse_gpu_num(g.get("fan_speed"))
        p_val = _parse_gpu_num(g.get("power_draw"))
        gu_val = _parse_gpu_num(g.get("gpu_util"))
        mu_val = _parse_gpu_num(g.get("mem_util"))

        t_cls  = _gpu_cls(t_val,  55, 65)
        f_cls  = _gpu_cls(f_val,  50, 75)
        p_cls  = _gpu_cls(p_val,  250, 380)
        gu_cls = _gpu_cls(gu_val, 30, 60)
        mu_cls = _gpu_cls(mu_val, 30, 60)

        gpu_html = f"""
      <div class="gpu-panel">
        <div class="gpu-name">{_esc(g.get('device_name', 'GPU'))}</div>
        <div class="gpu-metrics">
          <span class="{t_cls}">{_esc(g.get('temp', '??'))}</span>
          <span class="sep">•</span>
          <span class="{f_cls}">fan {_esc(g.get('fan_speed', '??'))}</span>
          <span class="sep">•</span>
          <span class="{p_cls}">pwr {_esc(g.get('power_draw', '??'))}</span>
          <span class="sep">•</span>
          <span class="{gu_cls}">gpu {_esc(g.get('gpu_util', '??'))}</span>
          <span class="sep">•</span>
          <span class="{mu_cls}">mem {_esc(g.get('mem_util', '??'))}</span>
        </div>
        <div class="gpu-bar-row">
          <span class="gpu-bar-lbl">gpu</span>
          <div class="mini-track"><div class="mini-fill {gu_cls}" style="width:{min(100, gu_val):.0f}%"></div></div>
          <span class="gpu-bar-lbl">mem</span>
          <div class="mini-track"><div class="mini-fill {mu_cls}" style="width:{min(100, mu_val):.0f}%"></div></div>
        </div>
      </div>"""

    return f"""<div class="slot-header"
     hx-get="/api/header"
     hx-trigger="every 2s"
     hx-swap="outerHTML">
  <div class="panel">
    <div class="panel-title">VBC</div>
    <div class="hdr-body">
      <div class="hdr-left">
        <div class="status-row">
          <span class="dot {dot_cls}"></span>
          <span class="badge {badge_cls}">{_esc(label)}</span>
          <span class="dim">Threads: {threads_disp}</span>
        </div>
        <div class="kpi-row">
          ETA: {_esc(eta_str)}<span class="sep">•</span>{_esc(tp_str)}<span class="sep">•</span><span class="accent">{saved_str} saved ({ratio_pct:.1f}%)</span>
        </div>
        <div class="hint">Read-only web dashboard &mdash; updates every 2s</div>
      </div>
      {gpu_html}
    </div>
  </div>
</div>"""


def _render_progress(s: dict) -> str:
    pct = min(100.0, max(0.0, s["pct_global"]))
    total = s["files_to_process"]
    done = s["completed"]
    failed = s["failed"]
    src = s["source_folders"]

    hdr = f"Done: {done}/{total}"
    if src > 1:
        hdr += f"<span class='sep'>•</span>Sources: {src}"
    fail_span = f"<span class='sep'>•</span><span class='fail'>Failed: {failed}</span>" if failed else ""

    tp_str = f"{s['throughput_bps'] / 1_048_576:.1f} MB/s"
    total_sz = _esc(_fmt_size(s["total_size"]))
    done_sz = _esc(_fmt_size(s["total_in"]))
    elapsed_str = _esc(_fmt_time(s["elapsed"]) if s["elapsed"] > 0 else "--:--")
    eta_str = _esc(_fmt_time(s["eta_seconds"]))

    return f"""<div class="slot-progress"
     hx-get="/api/progress"
     hx-trigger="every 2s"
     hx-swap="outerHTML">
  <div class="panel">
    <div class="panel-title">PROGRESS</div>
    <div class="prog-header">{hdr}{fail_span}</div>
    <div class="bar-wrap">
      <div class="bar-track">
        <div class="bar-fill bar-fill-global" style="width:{pct:.1f}%"></div>
      </div>
      <span class="bar-pct">{pct:.1f}%</span>
    </div>
    <div class="bar-meta">
      {done_sz}/{total_sz}<span class="sep">•</span>{_esc(tp_str)}<span class="sep">•</span>{elapsed_str}<span class="sep">•</span>ETA {eta_str}
    </div>
  </div>
</div>"""


def _render_active_jobs(s: dict) -> str:
    jobs = s["active_jobs"]
    now = s["now"]

    if not jobs:
        return """<div class="slot-active"
     hx-get="/api/active"
     hx-trigger="every 2s"
     hx-swap="outerHTML">
  <div class="panel">
    <div class="panel-title">ACTIVE JOBS</div>
    <div class="empty">No active jobs</div>
  </div>
</div>"""

    rows = []
    for job in jobs:
        fname = _esc(job.source_file.path.name)
        meta = job.source_file.metadata
        dur = _fmt_time(getattr(meta, "duration", None) if meta else None)
        fps = _fmt_fps(meta)
        size = _fmt_size(job.source_file.size_bytes)
        q = _quality_str(job)
        pct = min(100.0, max(0.0, float(job.progress_percent or 0.0)))

        # Per-job ETA
        eta_str = "--:--"
        key = job.source_file.path.name
        if key in s["job_start_times"] and 0 < pct < 100:
            job_elapsed = (now - s["job_start_times"][key]).total_seconds()
            if job_elapsed > 0:
                eta_str = _fmt_time((job_elapsed / pct) * (100.0 - pct))

        meta_parts = []
        if dur != "--:--":
            meta_parts.append(f"dur {dur}")
        if fps:
            meta_parts.append(fps)
        meta_parts.append(f"in {size}")
        if q:
            meta_parts.append(f"\u2192 {q}")

        rotation = getattr(job, "rotation_angle", None) or 0
        custom_cq = getattr(meta, "custom_cq", None) if meta else None
        spin_char = _esc(_spinner(job.source_file.path.name, rotation, custom_cq))

        rows.append(f"""    <div class="job-row">
      <div class="job-name-row">
        <span class="job-dot">{spin_char}</span>
        <span class="job-name">{fname}</span>
        <span class="job-meta">{_esc(" \u2022 ".join(meta_parts))}</span>
      </div>
      <div class="job-bar-row">
        <div class="bar-track job-bar">
          <div class="bar-fill bar-fill-job" style="width:{pct:.1f}%"></div>
        </div>
        <span class="job-pct">{pct:>5.1f}%</span>
        <span class="sep">•</span>
        <span class="job-eta">{_esc(eta_str)}</span>
      </div>
    </div>""")

    body = "\n".join(rows)
    return f"""<div class="slot-active"
     hx-get="/api/active"
     hx-trigger="every 2s"
     hx-swap="outerHTML">
  <div class="panel">
    <div class="panel-title">ACTIVE JOBS</div>
{body}
  </div>
</div>"""


def _render_activity(s: dict) -> str:
    jobs = s["recent_jobs"]

    if not jobs:
        return """<div class="slot-activity"
     hx-get="/api/activity"
     hx-trigger="every 2s"
     hx-swap="outerHTML">
  <div class="panel">
    <div class="panel-title">ACTIVITY FEED</div>
    <div class="empty">No recent jobs</div>
  </div>
</div>"""

    rows = []
    for job in jobs[:5]:
        fname = _esc(job.source_file.path.name)
        raw_status = getattr(job, "status", None)
        status = raw_status.value if hasattr(raw_status, "value") else str(raw_status)

        if status == "COMPLETED":
            in_b = job.source_file.size_bytes or 0
            out_b = getattr(job, "output_size_bytes", None) or 0
            ratio_pct = ((in_b - out_b) / in_b * 100) if in_b > 0 else 0
            q = _quality_str(job)
            dur = _fmt_time(getattr(job, "duration_seconds", None))
            src = getattr(getattr(job, "config_source", None), "value", "G")
            stat_parts = [src]
            if q:
                stat_parts.append(q)
            stat_parts.append(f"{_fmt_size(in_b)} \u2192 {_fmt_size(out_b)} ({ratio_pct:.1f}%)")
            stat_parts.append(dur)
            rows.append(f"""    <div class="act-row">
      <span class="act-icon ok">&#10003;</span>
      <div class="act-body">
        <div class="act-name">{fname}</div>
        <div class="act-stat ok">{_esc(" \u2022 ".join(stat_parts))}</div>
      </div>
    </div>""")

        elif status == "FAILED":
            err = _esc(getattr(job, "error_message", None) or "error")
            rows.append(f"""    <div class="act-row">
      <span class="act-icon fail">&#10007;</span>
      <div class="act-body">
        <div class="act-name">{fname}</div>
        <div class="act-stat fail">{err}</div>
      </div>
    </div>""")

        elif status == "INTERRUPTED":
            rows.append(f"""    <div class="act-row">
      <span class="act-icon warn">&#9889;</span>
      <div class="act-body">
        <div class="act-name">{fname}</div>
        <div class="act-stat warn">INTERRUPTED</div>
      </div>
    </div>""")

        else:
            rows.append(f"""    <div class="act-row">
      <span class="act-icon dim">&#8801;</span>
      <div class="act-body">
        <div class="act-name">{fname}</div>
        <div class="act-stat dim">{_esc(status)}</div>
      </div>
    </div>""")

    body = "\n".join(rows)
    return f"""<div class="slot-activity"
     hx-get="/api/activity"
     hx-trigger="every 2s"
     hx-swap="outerHTML">
  <div class="panel">
    <div class="panel-title">ACTIVITY FEED</div>
{body}
  </div>
</div>"""


def _render_queue(s: dict) -> str:
    files = s["pending_files"]

    if not files:
        return """<div class="slot-queue"
     hx-get="/api/queue"
     hx-trigger="every 2s"
     hx-swap="outerHTML">
  <div class="panel">
    <div class="panel-title">QUEUE</div>
    <div class="empty">Queue empty</div>
  </div>
</div>"""

    MAX_DISPLAY = 5
    shown = files[:MAX_DISPLAY]
    more = len(files) - MAX_DISPLAY if len(files) > MAX_DISPLAY else 0

    rows = []
    for f in shown:
        fname = _esc(f.path.name)
        size = _fmt_size(getattr(f, "size_bytes", None))
        fps = _fmt_fps(getattr(f, "metadata", None))
        meta_parts = [p for p in [size, fps] if p]
        meta = _esc(" \u2022 ".join(meta_parts))
        rows.append(f"""    <div class="q-item">
      <div class="q-name-row"><span class="q-arrow">&raquo;</span><span class="q-name">{fname}</span></div>
      <div class="q-meta">{meta}</div>
    </div>""")

    if more > 0:
        rows.append(f'    <div class="q-more">&hellip; +{more} more</div>')

    body = "\n".join(rows)
    title = f"QUEUE ({len(files)} files)"
    return f"""<div class="slot-queue"
     hx-get="/api/queue"
     hx-trigger="every 2s"
     hx-swap="outerHTML">
  <div class="panel">
    <div class="panel-title">{title}</div>
{body}
  </div>
</div>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class VBCRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for VBC web dashboard.

    Class attribute ``state`` is set by VBCWebServer before the server starts.
    """

    state: "UIState"  # Injected by VBCWebServer.start()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        """Suppress default access log to keep VBC terminal clean."""

    def _send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                self._send_html(INDEX_HTML)
                return

            s = _compute_stats(self.__class__.state)

            if path == "/api/header":
                self._send_html(_render_header(s))
            elif path == "/api/progress":
                self._send_html(_render_progress(s))
            elif path == "/api/active":
                self._send_html(_render_active_jobs(s))
            elif path == "/api/activity":
                self._send_html(_render_activity(s))
            elif path == "/api/queue":
                self._send_html(_render_queue(s))
            else:
                self._send_html("<h1>404</h1>", status=404)

        except Exception as exc:
            logger.debug("Web dashboard request error for %s: %s", path, exc)
            try:
                self._send_html(
                    f"<div class='panel'><div class='panel-title'>ERROR</div>"
                    f"<div class='fail'>{_esc(str(exc))}</div></div>",
                    status=500,
                )
            except Exception:
                pass


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Thread-per-request HTTP server with address reuse and daemon threads."""

    allow_reuse_address = True
    daemon_threads = True  # request threads die when main thread exits


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class VBCWebServer:
    """Read-only HTMX web dashboard server for VBC.

    Runs as a daemon thread — stops automatically when VBC process exits.

    Usage::

        server = VBCWebServer(state=ui_state, port=8765)
        server.start()   # non-blocking, prints URL
        # ... VBC runs ...
        server.stop()    # optional; daemon thread auto-stops on exit
    """

    def __init__(self, state: "UIState", port: int = DEFAULT_PORT, host: str = "0.0.0.0") -> None:
        self.state = state
        self.port = port
        self.host = host
        self._server: Optional[_ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start web server in a daemon background thread."""
        VBCRequestHandler.state = self.state  # inject shared state
        try:
            self._server = _ThreadingHTTPServer((self.host, self.port), VBCRequestHandler)
        except OSError as exc:
            logger.warning("Web dashboard: could not bind to %s:%d: %s", self.host, self.port, exc)
            print(f"[VBC] Web dashboard: {self.host}:{self.port} unavailable — dashboard disabled.")
            return

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="vbc-web-dashboard",
            daemon=True,
        )
        self._thread.start()
        display_host = "localhost" if self.host in ("0.0.0.0", "::") else self.host
        logger.info("Web dashboard: http://%s:%d/", display_host, self.port)
        print(f"[VBC] Web dashboard: http://{display_host}:{self.port}/")

    def stop(self) -> None:
        """Gracefully stop the web server."""
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
