import threading
from datetime import datetime
from collections import deque
from typing import List, Optional, Dict, Any, ClassVar, Tuple
from vbc.domain.models import CompressionJob
from vbc.ui.gpu_sparkline import (
    DEFAULT_GPU_SPARKLINE_PALETTE,
    DEFAULT_GPU_SPARKLINE_PRESET,
)

class UIState:
    """Thread-safe state manager for the interactive UI."""

    # Tab order for cycling (shortcuts first as it's the main menu)
    OVERLAY_TABS: ClassVar[List[str]] = ["shortcuts", "settings", "io", "tui", "reference"]
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

        # Job lists
        self.active_jobs: List[CompressionJob] = []
        self.recent_jobs = deque(maxlen=activity_feed_max_items)
        self.pending_files: List[Any] = []  # VideoFile objects waiting to be submitted

        # Job timing tracking
        self.job_start_times: Dict[str, datetime] = {}  # filename -> start time

        # Global Status
        self.discovery_finished = False
        self.discovery_in_progress = False
        self.discovery_finished_time: Optional[datetime] = None
        self.total_files_found = 0
        self.current_threads = 0
        self.source_folders_count = 1
        self.shutdown_requested = False
        self.interrupt_requested = False
        self.finished = False
        self.strip_unicode_display = True
        self.ui_title = "VBC"
        # Tabbed overlay state
        self.show_overlay = False
        self.active_tab = "shortcuts"  # "shortcuts" | "settings" | "io" | "tui" | "reference"
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

    def add_active_job(self, job: CompressionJob):
        with self._lock:
            if job not in self.active_jobs:
                self.active_jobs.append(job)
                # Track start time
                self.job_start_times[job.source_file.path.name] = datetime.now()

    def remove_active_job(self, job: CompressionJob):
        with self._lock:
            if job in self.active_jobs:
                self.active_jobs.remove(job)
            # Clean up start time
            self.job_start_times.pop(job.source_file.path.name, None)

    def add_completed_job(self, job: CompressionJob, output_size: int):
        with self._lock:
            self.completed_count += 1
            self.total_input_bytes += job.source_file.size_bytes
            self.total_output_bytes += output_size
            self.throughput_history.append((datetime.now(), job.source_file.size_bytes))
            
            # Prune history older than 60s
            cutoff = datetime.now().timestamp() - 60
            while self.throughput_history and self.throughput_history[0][0].timestamp() < cutoff:
                self.throughput_history.popleft()
                
            # Store output size in job for display
            job.output_size_bytes = output_size
            self.recent_jobs.appendleft(job)
            self.remove_active_job(job)

    def add_failed_job(self, job: CompressionJob):
        with self._lock:
            self.failed_count += 1
            self.throughput_history.append((datetime.now(), 0))
            # Prune history older than 60s
            cutoff = datetime.now().timestamp() - 60
            while self.throughput_history and self.throughput_history[0][0].timestamp() < cutoff:
                self.throughput_history.popleft()
            self.recent_jobs.appendleft(job)
            self.remove_active_job(job)

    def add_skipped_job(self, job: CompressionJob):
        with self._lock:
            self.skipped_count += 1
            self.remove_active_job(job)

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
