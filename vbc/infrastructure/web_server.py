"""Read-only HTMX web dashboard for VBC.

Serves a single-page dashboard that auto-refreshes via HTMX polling every 2s.
Runs as a daemon thread — stops automatically when VBC exits.

No new dependencies: uses stdlib http.server + socketserver only.
HTMX 2.0.8 and Pico.css 2.1.1 loaded from jsDelivr CDN.
Static files (style.css, theme-switcher.js) served from vbc/infrastructure/web/.
HTML fragments rendered via Jinja2 templates in vbc/infrastructure/web/templates/.
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
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

if TYPE_CHECKING:
    from vbc.ui.state import UIState

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765

# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "web"
_ALLOWED_MIME = {".css": "text/css", ".js": "application/javascript"}

_TEMPLATE_DIR = _STATIC_DIR / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

def _get_index_html() -> str:
    return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Format helpers (pure functions, mirror dashboard.py conventions)
# ---------------------------------------------------------------------------

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


# Register format helpers as Jinja2 globals so templates can call them directly.
_jinja_env.globals.update(fmt_size=_fmt_size, fmt_time=_fmt_time)


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


def _compact_activity_error(error_message: object) -> str:
    """Shorten verbose verification errors for Activity Feed readability."""
    text = str(error_message or "error").replace("\n", " ").strip()
    marker = "No video stream found in "
    if "Verification failed:" in text and marker in text:
        return f"{text.split(marker, 1)[0]}No video stream found"
    return text


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
        is_waiting = state.waiting_for_input
        is_finished = state.finished
        is_interrupted = state.interrupt_requested
        is_shutdown = state.shutdown_requested
        is_error_paused = state.error_paused
        error_message = state.error_message

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
        if rem > 0:
            avg_sec_per_file = 0.0
            if files_window > 0 and time_window > 0:
                avg_sec_per_file = time_window / files_window
            elif (completed + failed) > 0 and elapsed > 0:
                # Fallback: global average when sliding window is empty
                avg_sec_per_file = elapsed / (completed + failed)
            if avg_sec_per_file > 0:
                eta_seconds = avg_sec_per_file * rem

        # Global progress % by bytes
        pending_bytes = sum(getattr(f, "size_bytes", 0) for f in pending_files)
        active_bytes = sum(getattr(j.source_file, "size_bytes", 0) for j in active_jobs)
        total_size = pending_bytes + active_bytes + total_in
        pct_global = (total_in / total_size * 100.0) if total_size > 0 else 0.0

        space_saved = max(0, total_in - total_out)
        ratio = (total_out / total_in) if total_in > 0 else 0.0
        active_count = len(active_jobs)
        target_threads = 0 if (is_shutdown or is_waiting or is_error_paused) else state.current_threads
        source_folders = state.source_folders_count

        hw_cap_count       = state.hw_cap_count
        skipped_count      = state.skipped_count
        cam_skipped_count  = state.cam_skipped_count
        kept_count         = state.min_ratio_skip_count
        ignored_small_count = state.ignored_small_count

    return {
        "now": now,
        "completed": completed,
        "failed": failed,
        "files_to_process": files_to_process,
        "is_waiting": is_waiting,
        "is_finished": is_finished,
        "is_interrupted": is_interrupted,
        "is_shutdown": is_shutdown,
        "is_error_paused": is_error_paused,
        "error_message": error_message,
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
        "hw_cap_count":        hw_cap_count,
        "skipped_count":       skipped_count,
        "cam_skipped_count":   cam_skipped_count,
        "kept_count":          kept_count,
        "ignored_small_count": ignored_small_count,
    }


# ---------------------------------------------------------------------------
# View-model functions (pure dicts, no HTML) + Jinja2 template renderers
# ---------------------------------------------------------------------------

def _vm_header(s: dict) -> dict:
    if s["is_error_paused"]:
        badge_cls, label = "badge-interrupt", "ERROR"
    elif s["is_waiting"]:
        badge_cls, label = "badge-waiting", "WAITING"
    elif s["is_finished"]:
        badge_cls, label = "badge-done", "FINISHED"
    elif s["is_interrupted"]:
        badge_cls, label = "badge-interrupt", "INTERRUPTED"
    elif s["is_shutdown"]:
        badge_cls, label = "badge-shutdown", "SHUTTING DOWN"
    else:
        badge_cls, label = "badge-active", "ACTIVE"

    a, t = s["active_count"], s["target_threads"]
    threads_disp = str(a) if a == t else f"{a} \u2192 {t}"

    tp_str = f"{s['throughput_bps'] / 1_048_576:.1f} MB/s"
    eta_str = _fmt_time(s["eta_seconds"])
    saved_str = _fmt_size(s["space_saved"])
    ratio_pct = (1.0 - s["ratio"]) * 100.0

    counters = [
        ("fail",   s["failed"],              "FFmpeg compression failed"),
        ("err",    s["skipped_count"],        "Skipped due to .err marker file"),
        ("hw_cap", s["hw_cap_count"],         "GPU capacity exceeded, fell back to CPU"),
        ("skip",   s["cam_skipped_count"],    "Skipped by camera filter"),
        ("kept",   s["kept_count"],           "Original kept (compression ratio too low)"),
        ("small",  s["ignored_small_count"],  "File too small, ignored"),
    ]
    stats = [(lbl, val, tip) for lbl, val, tip in counters if val > 0]

    return {
        "badge_cls":    badge_cls,
        "label":        label,
        "threads_disp": threads_disp,
        "eta_str":      eta_str,
        "tp_str":       tp_str,
        "saved_str":    saved_str,
        "ratio_pct":    ratio_pct,
        "stats":        stats,
    }


def _render_header(s: dict) -> str:
    return _jinja_env.get_template("header.html").render(**_vm_header(s))


def _vm_gpu(s: dict) -> dict:
    g = s["gpu_data"]
    if not g:
        return {"gpu": None}
    t_val  = _parse_gpu_num(g.get("temp"))
    f_val  = _parse_gpu_num(g.get("fan_speed"))
    p_val  = _parse_gpu_num(g.get("power_draw"))
    gu_val = _parse_gpu_num(g.get("gpu_util"))
    mu_val = _parse_gpu_num(g.get("mem_util"))
    return {"gpu": {
        "device_name": g.get("device_name", "GPU"),
        "temp":        g.get("temp", "??"),
        "t_cls":       _gpu_cls(t_val,  55, 65),
        "fan_speed":   g.get("fan_speed", "??"),
        "f_cls":       _gpu_cls(f_val,  50, 75),
        "power_draw":  g.get("power_draw", "??"),
        "p_cls":       _gpu_cls(p_val,  250, 380),
        "gpu_util":    g.get("gpu_util", "??"),
        "gu_cls":      _gpu_cls(gu_val, 30, 60),
        "gu_val":      min(100.0, gu_val),
        "mem_util":    g.get("mem_util", "??"),
        "mu_cls":      _gpu_cls(mu_val, 30, 60),
        "mu_val":      min(100.0, mu_val),
    }}


def _render_gpu(s: dict) -> str:
    return _jinja_env.get_template("gpu.html").render(**_vm_gpu(s))


def _vm_progress(s: dict) -> dict:
    pct = min(100.0, max(0.0, s["pct_global"]))
    tp_str = f"{s['throughput_bps'] / 1_048_576:.1f} MB/s"
    return {
        "pct":          pct,
        "done":         s["completed"],
        "total":        s["files_to_process"],
        "failed":       s["failed"],
        "src":          s["source_folders"],
        "done_sz":      _fmt_size(s["total_in"]),
        "total_sz":     _fmt_size(s["total_size"]),
        "tp_str":       tp_str,
        "elapsed_str":  _fmt_time(s["elapsed"]) if s["elapsed"] > 0 else "--:--",
        "eta_str":      _fmt_time(s["eta_seconds"]),
    }


def _render_progress(s: dict) -> str:
    return _jinja_env.get_template("progress.html").render(**_vm_progress(s))


def _vm_active_jobs(s: dict) -> dict:
    now = s["now"]
    job_items = []
    for job in s["active_jobs"]:
        fname = job.source_file.path.name
        meta = job.source_file.metadata
        dur = _fmt_time(getattr(meta, "duration", None) if meta else None)
        fps = _fmt_fps(meta)
        size = _fmt_size(job.source_file.size_bytes)
        q = _quality_str(job)
        pct = min(100.0, max(0.0, float(job.progress_percent or 0.0)))

        eta_str = "--:--"
        if pct >= 100:
            eta_str = "0s"
        elif fname in s["job_start_times"] and pct > 0:
            job_elapsed = (now - s["job_start_times"][fname]).total_seconds()
            if job_elapsed > 0:
                eta_str = _fmt_time((job_elapsed / pct) * (100.0 - pct))

        meta_parts = []
        if dur != "--:--":
            meta_parts.append(f"dur {dur}")
        if fps:
            meta_parts.append(fps)
        meta_parts.append(f"in {size}")
        rotation = getattr(job, "rotation_angle", None) or 0
        custom_cq = getattr(meta, "custom_cq", None) if meta else None
        meta_str = " • ".join(meta_parts)
        if q:
            meta_str += f" → {q}"
        job_items.append({
            "fname":   fname,
            "spin":    _spinner(fname, rotation, custom_cq),
            "meta":    meta_str,
            "pct":     pct,
            "eta_str": eta_str,
        })
    return {"jobs": job_items}


def _render_active_jobs(s: dict) -> str:
    return _jinja_env.get_template("active_jobs.html").render(**_vm_active_jobs(s))


def _vm_activity(s: dict) -> dict:
    job_items = []
    for job in s["recent_jobs"][:5]:
        fname = job.source_file.path.name
        raw_status = getattr(job, "status", None)
        status = raw_status.value if hasattr(raw_status, "value") else str(raw_status)

        stat_str = None
        error = None
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
            stat_str = " \u2022 ".join(stat_parts)
        elif status == "FAILED":
            error = _compact_activity_error(getattr(job, "error_message", None))
        elif status != "INTERRUPTED":
            stat_str = status

        job_items.append({
            "fname":    fname,
            "status":   status,
            "stat_str": stat_str,
            "error":    error,
            "verified": bool(getattr(job, "verification_passed", False)),
        })
    return {"jobs": job_items}


def _render_activity(s: dict) -> str:
    return _jinja_env.get_template("activity.html").render(**_vm_activity(s))


def _vm_queue(s: dict) -> dict:
    files = s["pending_files"]
    MAX_DISPLAY = 5
    more = max(0, len(files) - MAX_DISPLAY)
    items = []
    for f in files[:MAX_DISPLAY]:
        size = _fmt_size(getattr(f, "size_bytes", None))
        fps = _fmt_fps(getattr(f, "metadata", None))
        meta_parts = [p for p in [size, fps] if p]
        items.append({
            "fname": f.path.name,
            "meta":  " \u2022 ".join(meta_parts),
        })
    return {
        "title": f"QUEUE ({len(files)} files)" if files else "QUEUE",
        "items": items,
        "more":  more,
    }


def _render_queue(s: dict) -> str:
    return _jinja_env.get_template("queue.html").render(**_vm_queue(s))


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

    def _send_static(self, filename: str) -> None:
        """Serve a static file from the web/ directory."""
        filepath = (_STATIC_DIR / filename).resolve()
        # Path traversal guard (is_relative_to avoids prefix-match false positives)
        if not filepath.is_relative_to(_STATIC_DIR.resolve()):
            self._send_html("<h1>403</h1>", status=403)
            return
        if not filepath.exists():
            self._send_html("<h1>404</h1>", status=404)
            return
        content_type = _ALLOWED_MIME.get(filepath.suffix, "application/octet-stream")
        data = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                self._send_html(_get_index_html())
                return

            if path.startswith("/static/"):
                self._send_static(path[8:])
                return

            s = _compute_stats(self.__class__.state)

            if path == "/api/header":
                self._send_html(_render_header(s))
            elif path == "/api/gpu":
                self._send_html(_render_gpu(s))
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
                    f"<article><header>ERROR</header>"
                    f"<p class='fail'>{html.escape(str(exc))}</p></article>",
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
