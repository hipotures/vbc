"""Client-side state of `vbc tui` (copied from vbc_legacy/ui/state.py).

Same attribute surface as the legacy ``UIState`` so the copied renderers
work unchanged, but the business half is no longer computed here: it is
filled from API responses by ``apply_*`` (called by ``poller.Poller``).
View state (overlay, tabs, Dirs editor, logs paging, sparkline selection)
stays client-only. Active-job progress is extrapolated from the server's
anchors on every read (dead reckoning).
"""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from collections import deque
from typing import List, Optional, Dict, Any, ClassVar, Tuple, Set

from vbc import contracts as dto
from vbc.clients.tui.gpu_sparkline import (
    DEFAULT_GPU_SPARKLINE_PALETTE,
    DEFAULT_GPU_SPARKLINE_PRESET,
)
from vbc.clients.tui.views import FileView, JobView, SessionErrorEntry, local_datetime

logger = logging.getLogger("vbc.tui")

STATUS_OK = "✓"
STATUS_MISSING = "✗"
STATUS_NO_ACCESS = "⚡"
MAX_INPUT_DIR_LEN = 150


class ClientState:
    """Thread-safe state of the TUI client."""

    # Tab order for cycling (shortcuts first as it's the main menu)
    OVERLAY_TABS: ClassVar[List[str]] = ["shortcuts", "settings", "io", "dirs", "tui", "reference", "logs"]
    OVERLAY_DIM_LEVELS: ClassVar[List[str]] = ["light", "mid", "dark"]

    def __init__(self, activity_feed_max_items: int = 5):
        self._lock = threading.RLock()

        # Counters
        self.completed_count = 0
        self.failed_count = 0
        self.skipped_count = 0
        self.hw_cap_count = 0
        self.cam_skipped_count = 0
        self.min_ratio_skip_count = 0  # Files copied instead of compressed (ratio too low)
        self.interrupted_count = 0  # Files interrupted by Ctrl+C
        self.completed_count_at_last_discovery = 0
        self.failed_count_at_last_discovery = 0
        self.session_completed_base = 0

        # Discovery counters (files skipped before processing)
        self.files_to_process = 0
        self.already_compressed_count = 0
        self.ignored_small_count = 0
        self.ignored_err_count = 0
        self.ignored_av1_count = 0

        # Bytes tracking
        self.total_input_bytes = 0
        self.total_output_bytes = 0
        self.throughput_history: deque[Tuple[datetime, int]] = deque()

        # Job lists (filled from /state and /queue)
        self._active: List[Tuple[JobView, Optional[dto.ProgressAnchor]]] = []
        self.recent_jobs = deque(maxlen=activity_feed_max_items)
        self.pending_files: List[Any] = []  # first page of /queue (FileView)
        self.queue_count = 0
        self.queue_bytes = 0

        # Server-computed anchors (replace the legacy local throughput history)
        self.throughput_bps: Optional[float] = None
        self.eta_at: Optional[datetime] = None

        # Connection to the server
        self.server_url = ""
        self.connected = False
        self.connection_error: Optional[str] = None  # short reason, e.g. "connection refused"
        self.ever_connected = False
        self.clock_offset_ms = 0
        self.rev = 0
        self.dirs_rev = 0
        self.config_rev = 0
        self.errors_last_id = 0
        self.gpu_last_seq = 0
        self.dirs_status: Dict[str, Tuple[bool, bool]] = {}  # path -> (exists, readable) on the server
        self.run_status = ""
        self.stopped = False
        self.stopping = False
        self.server_exiting = False

        # Job timing tracking
        self.job_start_times: Dict[str, datetime] = {}  # filename -> start time

        # Global Status
        self.discovery_finished = False
        self.discovery_finished_time: Optional[datetime] = None
        self.total_files_found = 0
        self.current_threads = 0
        self.source_folders_count = 1
        self.shutdown_requested = False
        self.interrupt_requested = False
        self.error_paused = False
        self.error_status_text: Optional[str] = None
        self.error_message: Optional[str] = None
        self.repair_active = False
        self.repair_candidate_count = 0
        self.repair_repaired_count = 0
        self.finished = False
        self.waiting_for_input = False
        self.strip_unicode_display = True
        self.ui_title = "VBC"
        # Tabbed overlay state
        self.show_overlay = False
        self.active_tab = "shortcuts"  # "shortcuts" | "settings" | "io" | "dirs" | "tui" | "reference" | "logs"
        self.overlay_dim_level = "mid"  # "light" | "mid" | "dark"
        self.show_info = False
        self.info_message = ""
        self.config_lines: List[str] = []
        self.io_input_dir_stats: List[Tuple[str, str, Optional[int], Optional[int]]] = []
        self.io_output_dir_lines: List[str] = []
        self.io_errors_dir_lines: List[str] = []
        self.io_suffix_output_dirs: Optional[str] = None
        self.io_suffix_errors_dirs: Optional[str] = None
        self.io_queue_sort: str = "name"
        self.io_queue_seed: Optional[int] = None
        self.log_path: Optional[str] = None
        self.debug_enabled: bool = False
        self.processing_start_time: Optional[datetime] = None

        self.last_action: str = ""
        self.last_action_time: Optional[datetime] = None

        # Dirs tab state
        self.dirs_cursor: int = 0
        self.dirs_input_mode: bool = False
        self.dirs_input_buffer: str = ""
        self.dirs_pending_toggle: Dict[str, bool] = {}   # path → new enabled state (True=enable, False=disable)
        self.dirs_pending_add: List[str] = []
        self.dirs_pending_remove: Set[str] = set()
        self.dirs_pending_order: List[str] = []          # staged row order (applies on [S])
        self.dirs_config_entries: List[Tuple[str, bool]] = []  # ordered config input_dirs: (path, enabled)
        self.dirs_error_msg: str = ""  # error shown inside Dirs overlay

        # GPU Metrics
        self.gpu_data: Optional[Dict[str, Any]] = None

        # GPU Sparkline
        self.gpu_sparkline_metric_idx: int = 0  # Index into active GPU sparkline metric order
        self.gpu_sparkline_preset: str = DEFAULT_GPU_SPARKLINE_PRESET
        self.gpu_sparkline_palette: str = DEFAULT_GPU_SPARKLINE_PALETTE
        self.gpu_sparkline_mode: str = "sparkline"  # sparkline | palette
        self.gpu_history_temp: deque = deque(maxlen=60)
        self.gpu_history_pwr: deque = deque(maxlen=60)
        self.gpu_history_gpu: deque = deque(maxlen=60)
        self.gpu_history_mem: deque = deque(maxlen=60)
        self.gpu_history_fan: deque = deque(maxlen=60)
        self.gpu_enabled = False

        # Logs tab (current session only)
        self.logs_page_size: int = 10
        self.logs_page_index: int = 0
        self.session_error_logs: List[SessionErrorEntry] = []
        self._discovery_error_keys: set[Tuple[Path, str]] = set()

    # ── API -> state ───────────────────────────────────────────────────────

    def _server_now_ms(self) -> int:
        return int(time.time() * 1000) + self.clock_offset_ms

    @property
    def active_jobs(self) -> List[JobView]:
        """Active jobs with progress extrapolated to now (dead reckoning)."""
        with self._lock:
            now = self._server_now_ms()
            jobs = []
            for job, anchor in self._active:
                if anchor is not None:
                    job.progress_percent = min(anchor.percent + anchor.rate * (now - anchor.at) / 1000, 99.5)
                jobs.append(job)
            return jobs

    def apply_snapshot(self, snap: dto.StateSnapshot, offset_ms: int) -> None:
        with self._lock:
            if not self.connected and self.ever_connected:
                logger.info(f"Reconnected to the server at {self.server_url}")
                self.last_action = "Reconnected to the server"
                self.last_action_time = datetime.now()
            elif not self.ever_connected:
                logger.info(f"Connected to the server at {self.server_url}")
            self.connected = True
            self.ever_connected = True
            self.connection_error = None
            self.clock_offset_ms = offset_ms
            self.rev = snap.rev
            c, d = snap.counters, snap.discovery
            self.completed_count = c.completed
            self.failed_count = c.failed
            self.skipped_count = c.skipped
            self.hw_cap_count = c.hw_cap
            self.cam_skipped_count = c.cam_skipped
            self.min_ratio_skip_count = c.min_ratio_skip
            self.interrupted_count = c.interrupted
            self.ignored_av1_count = c.ignored_av1
            self.completed_count_at_last_discovery = c.completed_at_last_discovery
            self.failed_count_at_last_discovery = c.failed_at_last_discovery
            self.session_completed_base = c.session_completed_base
            self.discovery_finished = d.finished
            self.discovery_finished_time = local_datetime(d.finished_at, offset_ms)
            self.total_files_found = d.files_found
            self.files_to_process = d.files_to_process
            self.already_compressed_count = d.already_compressed
            self.ignored_small_count = d.ignored_small
            self.ignored_err_count = d.ignored_err
            self.source_folders_count = d.source_folders_count
            self.total_input_bytes = snap.total_input_bytes
            self.total_output_bytes = snap.total_output_bytes
            self.throughput_bps = snap.throughput.bytes_per_s if snap.throughput else None
            self.eta_at = local_datetime(snap.eta_at, offset_ms)
            self.processing_start_time = local_datetime(snap.processing_started_at, offset_ms)
            self.current_threads = snap.threads.current
            status = snap.run_status
            self.run_status = status
            self.stopping = status == "stopping"
            self.stopped = status == "stopped"
            self.server_exiting = status == "shutting_down"
            # Renderers read `shutdown_requested` for the thread target (0 while stopping).
            self.shutdown_requested = self.stopping or self.server_exiting
            self.interrupt_requested = snap.interrupt_requested
            self.error_paused = status in ("paused_on_error", "verification_abort", "failed")
            self.error_status_text = "ERROR" if self.error_paused else None
            self.error_message = snap.status_message if self.error_paused else None
            self.repair_active = snap.repair.active
            self.repair_candidate_count = snap.repair.candidates
            self.repair_repaired_count = snap.repair.repaired
            # `vbc serve` waits for a refresh after every pass (or a stop): R resumes.
            self.waiting_for_input = status in ("idle", "stopped")
            self.finished = False
            self._active = [(JobView.from_active(j), j.progress) for j in snap.active_jobs]
            self.job_start_times = {
                j.name: local_datetime(j.started_at, offset_ms) for j in snap.active_jobs
            }
            self.recent_jobs.clear()
            for job in snap.recent_jobs[: self.recent_jobs.maxlen]:
                self.recent_jobs.append(JobView.from_recent(job))
            self.queue_count = snap.queue.count
            self.queue_bytes = snap.queue.bytes
            if snap.last_action is not None:
                self.last_action = snap.last_action.text
                self.last_action_time = local_datetime(snap.last_action.at, offset_ms)
            self.dirs_rev = snap.dirs_rev
            self.config_rev = snap.config_rev
            self.errors_last_id = snap.errors_last_id
            self.gpu_last_seq = snap.gpu_last_seq

    def apply_queue(self, page: dto.QueuePage) -> None:
        with self._lock:
            self.pending_files = [
                FileView(Path(item.path), item.size_bytes, None, item.part_count) for item in page.items
            ]

    def apply_errors(self, page: dto.ErrorsPage, offset_ms: int) -> None:
        with self._lock:
            known = len(self.session_error_logs)
            for entry in page.entries:
                self.session_error_logs.insert(0, SessionErrorEntry.from_dto(entry, offset_ms))
            if len(self.session_error_logs) != known:
                self.logs_page_index = 0

    def apply_dirs(self, state: dto.DirsState) -> None:
        with self._lock:
            self.dirs_config_entries = [(e.path, e.enabled) for e in state.entries]
            self.dirs_status = {e.path: (e.exists, e.readable) for e in state.entries}
            self.dirs_rev = state.dirs_rev

    def apply_io(self, io: dto.IoView) -> None:
        with self._lock:
            stats = []
            for row in io.dirs:
                if not row.enabled:
                    continue
                status = STATUS_OK if (row.exists and row.readable) else (STATUS_MISSING if not row.exists else STATUS_NO_ACCESS)
                stats.append((status, row.path, row.pending_count, row.pending_bytes))
            self.io_input_dir_stats = stats
            self.io_suffix_output_dirs = io.suffix_output_dirs
            self.io_suffix_errors_dirs = io.suffix_errors_dirs
            self.io_queue_sort = io.queue_sort
            self.io_queue_seed = io.queue_seed
            self.log_path = io.log_path
            if io.suffix_output_dirs is None:
                self.io_output_dir_lines = [
                    f"  {_icon(r.output_writable)}{i}. {r.output_dir}"
                    for i, r in enumerate([r for r in io.dirs if r.enabled], start=1) if r.output_dir
                ]
            if io.suffix_errors_dirs is None:
                self.io_errors_dir_lines = [
                    f"  {_icon(True)}{i}. {r.errors_dir}"
                    for i, r in enumerate([r for r in io.dirs if r.enabled], start=1) if r.errors_dir
                ]

    def apply_config_lines(self, lines: List[str], debug: bool) -> None:
        with self._lock:
            self.config_lines = lines
            self.debug_enabled = debug

    def apply_gpu(self, page: dto.GpuPage) -> None:
        with self._lock:
            self.gpu_enabled = page.enabled
            for sample in page.samples:
                self.gpu_data = sample.raw
                self.gpu_history_temp.append(sample.temp_c)
                self.gpu_history_pwr.append(sample.power_w)
                self.gpu_history_gpu.append(sample.gpu_util_pct)
                self.gpu_history_mem.append(sample.mem_util_pct)
                self.gpu_history_fan.append(sample.fan_pct)
            if page.samples:
                self.gpu_last_seq = page.samples[-1].seq

    def set_disconnected(self, reason: str) -> None:
        """Mark the server unreachable; logged once per outage, not per retry."""
        with self._lock:
            if self.connected or self.connection_error is None or self.connection_error != reason:
                logger.warning(f"Server unreachable at {self.server_url}: {reason}; retrying")
            self.connected = False
            self.connection_error = reason

    @property
    def space_saved_bytes(self) -> int:
        with self._lock:
            return max(0, self.total_input_bytes - self.total_output_bytes)

    @property
    def compression_ratio(self) -> float:
        with self._lock:
            if self.total_input_bytes == 0:
                return 0.0
            return self.total_output_bytes / self.total_input_bytes

    def set_last_action(self, action: str):
        """Set last action message with timestamp (like old vbc.py)."""
        with self._lock:
            self.last_action = action
            self.last_action_time = datetime.now()

    def get_last_action(self) -> str:
        """Get last action message (clears after 60 seconds, like old vbc.py)."""
        with self._lock:
            if self.last_action and self.last_action_time:
                elapsed = (datetime.now() - self.last_action_time).total_seconds()
                if elapsed > 60:  # Clear after 1 minute
                    self.last_action = ""
                    self.last_action_time = None
            return self.last_action

    def _dirs_base_order_unlocked(self) -> List[str]:
        """Return default visible Dirs order (config entries + pending adds)."""
        base = [path for path, _ in self.dirs_config_entries]
        for path in self.dirs_pending_add:
            if path not in base:
                base.append(path)
        return base

    def _dirs_effective_order_unlocked(self) -> List[str]:
        """Return visible Dirs order after staged reorder is applied."""
        base = self._dirs_base_order_unlocked()
        if not self.dirs_pending_order:
            return base

        base_set = set(base)
        ordered: List[str] = []
        seen: Set[str] = set()
        for path in self.dirs_pending_order:
            if path in base_set and path not in seen:
                ordered.append(path)
                seen.add(path)
        for path in base:
            if path not in seen:
                ordered.append(path)
        return ordered

    def dirs_effective_order(self) -> List[str]:
        """Return current row order used by Dirs UI (including staged reorder)."""
        with self._lock:
            return list(self._dirs_effective_order_unlocked())

    def dirs_set_pending_order(self, ordered_paths: List[str]) -> None:
        """Stage a new Dirs row order; clears staged order when equal to base order."""
        with self._lock:
            base = self._dirs_base_order_unlocked()
            base_set = set(base)
            normalized: List[str] = []
            seen: Set[str] = set()
            for path in ordered_paths:
                if path in base_set and path not in seen:
                    normalized.append(path)
                    seen.add(path)
            for path in base:
                if path not in seen:
                    normalized.append(path)

            if normalized == base:
                self.dirs_pending_order = []
            else:
                self.dirs_pending_order = normalized

    def dirs_has_pending_changes(self) -> bool:
        """Return True when Dirs tab contains any staged changes awaiting apply."""
        with self._lock:
            if self.dirs_pending_toggle or self.dirs_pending_add or self.dirs_pending_remove:
                return True
            return self._dirs_effective_order_unlocked() != self._dirs_base_order_unlocked()

    def dirs_get_all_entries(self) -> List[Tuple[str, str, Optional[int], Optional[int], Optional[str]]]:
        """Return combined list of (path, status, file_count, size_bytes, fs_status) for Dirs tab.

        Status values:
        - "active"            → green  — currently active dir
        - "disabled"          → dim    — currently disabled dir
        - "pending_add"       → yellow — staged for adding
        - "pending_remove"    → red    — staged for deletion
        - "pending_toggle_off"→ yellow — active, pending disable
        - "pending_toggle_on" → yellow — disabled, pending enable

        fs_status: "ok" | "missing" | "no_access" | None (for disabled/pending)
        """
        with self._lock:
            entries: List[Tuple[str, str, Optional[int], Optional[int], Optional[str]]] = []
            stats_by_path = {entry: (fs, count, size) for fs, entry, count, size in self.io_input_dir_stats}
            enabled_by_path = {path: enabled for path, enabled in self.dirs_config_entries}
            effective_order = self._dirs_effective_order_unlocked()
            pending_add_set = set(self.dirs_pending_add)

            for path in effective_order:
                if path in enabled_by_path:
                    enabled = enabled_by_path[path]
                    fs_entry = stats_by_path.get(path)
                    if fs_entry is not None:
                        fs, count, size = fs_entry
                        fs_status = "ok" if fs == STATUS_OK else ("missing" if fs == STATUS_MISSING else "no_access")
                    else:
                        # Server paths: existence comes from /dirs, never the client's filesystem.
                        exists, readable = self.dirs_status.get(path, (True, True))
                        fs_status = "missing" if not exists else ("no_access" if not readable else "ok")
                        count, size = None, None

                    if path in self.dirs_pending_remove:
                        entries.append((path, "pending_remove", count, size, fs_status))
                        continue

                    pending_enabled = self.dirs_pending_toggle.get(path)
                    if pending_enabled is not None:
                        if pending_enabled:
                            entries.append((path, "pending_toggle_on", count, size, fs_status))
                        else:
                            entries.append((path, "pending_toggle_off", count, size, fs_status))
                        continue

                    entries.append((path, "active" if enabled else "disabled", count, size, fs_status))
                    continue

                if path in pending_add_set:
                    entries.append((path, "pending_add", None, None, None))

            return entries

    def open_overlay(self, tab: Optional[str] = None) -> None:
        """Open overlay, optionally on a specific tab."""
        with self._lock:
            self.show_overlay = True
            if tab and tab in self.OVERLAY_TABS:
                self.active_tab = tab

    def close_overlay(self) -> None:
        """Close overlay."""
        with self._lock:
            self.show_overlay = False

    def toggle_overlay(self, tab: Optional[str] = None) -> None:
        """Toggle overlay. If open on different tab, switch tabs."""
        with self._lock:
            if not self.show_overlay:
                # Closed → Open
                self.show_overlay = True
                if tab:
                    self.active_tab = tab
            elif tab and self.active_tab != tab:
                # Open on different tab → Switch tab
                self.active_tab = tab
            else:
                # Open on same tab → Close
                self.show_overlay = False

    def cycle_tab(self, direction: int = 1) -> None:
        """Cycle through tabs. direction: 1=next, -1=previous."""
        with self._lock:
            if not self.show_overlay:
                # Closed → Open on first tab
                self.show_overlay = True
                return

            current_idx = self.OVERLAY_TABS.index(self.active_tab)
            next_idx = (current_idx + direction) % len(self.OVERLAY_TABS)
            self.active_tab = self.OVERLAY_TABS[next_idx]

    def cycle_overlay_dim_level(self, direction: int = 1) -> None:
        """Cycle overlay dim level. direction: 1=next, -1=previous."""
        with self._lock:
            current_idx = self.OVERLAY_DIM_LEVELS.index(self.overlay_dim_level)
            next_idx = (current_idx + direction) % len(self.OVERLAY_DIM_LEVELS)
            self.overlay_dim_level = self.OVERLAY_DIM_LEVELS[next_idx]

    @staticmethod
    def logs_total_pages(self) -> int:
        with self._lock:
            if not self.session_error_logs:
                return 1
            return ((len(self.session_error_logs) - 1) // self.logs_page_size) + 1

    def cycle_logs_page(self, direction: int) -> None:
        """Navigate logs pages. direction: 1=next, -1=prev."""
        with self._lock:
            total_pages = self.logs_total_pages()
            next_page = self.logs_page_index + direction
            if next_page < 0:
                next_page = 0
            if next_page > total_pages - 1:
                next_page = total_pages - 1
            self.logs_page_index = next_page

    def get_logs_page(self) -> Tuple[List[SessionErrorEntry], int, int, int]:
        """Return (entries, page_index, total_pages, total_entries)."""
        with self._lock:
            total_entries = len(self.session_error_logs)
            total_pages = self.logs_total_pages()
            page_index = min(self.logs_page_index, total_pages - 1)
            page_size = self.logs_page_size
            start = page_index * page_size
            end = start + page_size
            entries = list(self.session_error_logs[start:end])
            return entries, page_index, total_pages, total_entries


def _icon(ok: Optional[bool]) -> str:
    """Status icon markup as the legacy IO tab renders it."""
    return "[green]✓ [/]" if ok else "[red]⚡[/]"


UIState = ClientState  # name used by the copied renderers
