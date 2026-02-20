from datetime import datetime
from pathlib import Path
from typing import Optional
from vbc.infrastructure.event_bus import EventBus
from vbc.ui.state import UIState
from vbc.domain.events import (
    DiscoveryStarted, DiscoveryFinished,
    JobStarted, JobCompleted, JobFailed,
    JobProgressUpdated, HardwareCapabilityExceeded, QueueUpdated,
    ActionMessage, ProcessingFinished, RefreshFinished,
    ThreadControlEvent, RequestShutdown, InterruptRequested,
    WaitingForInput, RefreshRequested, InputDirsChanged,
    DirsCursorMove, DirsToggleSelected, DirsEnterAddMode, DirsMarkDelete,
    DirsInputChar, DirsConfirmAdd, DirsCancelInput, DirsApplyChanges,
)
from vbc.config.input_dirs import STATUS_OK
from vbc.ui.gpu_sparkline import (
    format_preset_label,
    get_gpu_sparkline_config,
    get_gpu_sparkline_palette,
    list_gpu_sparkline_palettes,
    list_gpu_sparkline_presets,
)
from vbc.ui.keyboard import (
    ToggleOverlayTab, CycleOverlayTab, CloseOverlay, CycleOverlayDim, RotateGpuMetric,
    CycleSparklinePreset, CycleSparklinePalette, CycleLogsPage,
)
from vbc.ui.modern_overlays import DirsOverlay

class UIManager:
    """Subscribes to EventBus and updates UIState."""

    def __init__(self, bus: EventBus, state: UIState, demo_mode: bool = False, config_path: Optional[Path] = None):
        self.bus = bus
        self.state = state
        self.demo_mode = demo_mode
        self.config_path = config_path
        self._setup_subscriptions()

    def _setup_subscriptions(self):
        self.bus.subscribe(DiscoveryStarted, self.on_discovery_started)
        self.bus.subscribe(DiscoveryFinished, self.on_discovery_finished)
        self.bus.subscribe(JobStarted, self.on_job_started)
        self.bus.subscribe(JobCompleted, self.on_job_completed)
        self.bus.subscribe(JobFailed, self.on_job_failed)
        self.bus.subscribe(JobProgressUpdated, self.on_job_progress)
        self.bus.subscribe(HardwareCapabilityExceeded, self.on_hw_cap_exceeded)
        self.bus.subscribe(ThreadControlEvent, self.on_thread_control)
        self.bus.subscribe(RequestShutdown, self.on_shutdown_request)
        self.bus.subscribe(InterruptRequested, self.on_interrupt_request)
        self.bus.subscribe(ToggleOverlayTab, self.on_toggle_overlay_tab)
        self.bus.subscribe(CycleOverlayTab, self.on_cycle_overlay_tab)
        self.bus.subscribe(CloseOverlay, self.on_close_overlay)
        self.bus.subscribe(CycleOverlayDim, self.on_cycle_overlay_dim)
        self.bus.subscribe(RotateGpuMetric, self.on_rotate_gpu_metric)
        self.bus.subscribe(CycleSparklinePreset, self.on_cycle_sparkline_preset)
        self.bus.subscribe(CycleSparklinePalette, self.on_cycle_sparkline_palette)
        self.bus.subscribe(CycleLogsPage, self.on_cycle_logs_page)
        self.bus.subscribe(QueueUpdated, self.on_queue_updated)
        self.bus.subscribe(ActionMessage, self.on_action_message)
        self.bus.subscribe(RefreshFinished, self.on_refresh_finished)
        self.bus.subscribe(ProcessingFinished, self.on_processing_finished)
        self.bus.subscribe(WaitingForInput, self.on_waiting_for_input)
        # Dirs tab events
        self.bus.subscribe(DirsCursorMove, self.on_dirs_cursor_move)
        self.bus.subscribe(DirsToggleSelected, self.on_dirs_toggle_selected)
        self.bus.subscribe(DirsEnterAddMode, self.on_dirs_enter_add_mode)
        self.bus.subscribe(DirsMarkDelete, self.on_dirs_mark_delete)
        self.bus.subscribe(DirsInputChar, self.on_dirs_input_char)
        self.bus.subscribe(DirsConfirmAdd, self.on_dirs_confirm_add)
        self.bus.subscribe(DirsCancelInput, self.on_dirs_cancel_input)
        self.bus.subscribe(DirsApplyChanges, self.on_dirs_apply_changes)

    def on_discovery_started(self, event: DiscoveryStarted):
        self.state.discovery_finished = False
        self.state.waiting_for_input = False
        self.state.finished = False

    def on_discovery_finished(self, event: DiscoveryFinished):
        # Debug: log when discovery counters are updated
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(
            f"UI: Updating discovery counters: to_process={event.files_to_process}, "
            f"already_compressed={event.already_compressed}, ignored_small={event.ignored_small}, "
            f"ignored_err={event.ignored_err}"
        )

        self.state.total_files_found = event.files_found
        self.state.files_to_process = event.files_to_process
        self.state.already_compressed_count = event.already_compressed
        self.state.ignored_small_count = event.ignored_small
        self.state.ignored_err_count = event.ignored_err
        self.state.ignored_av1_count = event.ignored_av1
        self.state.source_folders_count = event.source_folders_count
        for err_entry in event.ignored_err_entries:
            self.state.add_discovery_error(
                path=err_entry.path,
                size_bytes=err_entry.size_bytes,
                error_message=err_entry.error_message,
            )
        self.state.discovery_finished = True
        self.state.discovery_finished_time = datetime.now()
        self.state.completed_count_at_last_discovery = self.state.completed_count
        self.state.failed_count_at_last_discovery = self.state.failed_count

    def on_refresh_finished(self, event: RefreshFinished):
        if event.added <= 0 and event.removed <= 0:
            return
        with self.state._lock:
            self.state.session_completed_base = self.state.completed_count

    def on_thread_control(self, event: ThreadControlEvent):
        with self.state._lock:
            if self.state.shutdown_requested:
                return
            new_val = self.state.current_threads + event.change
            self.state.current_threads = max(1, min(8, new_val))

    def on_shutdown_request(self, event: RequestShutdown):
        with self.state._lock:
            # Toggle shutdown state (press S again to cancel)
            self.state.shutdown_requested = not self.state.shutdown_requested

    def on_interrupt_request(self, event: InterruptRequested):
        with self.state._lock:
            self.state.interrupt_requested = True

    def on_toggle_overlay_tab(self, event: ToggleOverlayTab):
        """Handle overlay toggle with optional tab selection."""
        self.state.toggle_overlay(event.tab)

    def on_cycle_overlay_tab(self, event: CycleOverlayTab):
        """Handle tab cycling."""
        self.state.cycle_tab(event.direction)

    def on_close_overlay(self, event: CloseOverlay):
        """Handle overlay close."""
        self.state.close_overlay()

    def on_cycle_overlay_dim(self, event: CycleOverlayDim):
        """Cycle overlay background dimming level (TUI tab only)."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "tui":
                return
        self.state.cycle_overlay_dim_level(event.direction)
        self.state.set_last_action(f"TUI: Overlay dim {self.state.overlay_dim_level}")

    def on_rotate_gpu_metric(self, event: RotateGpuMetric):
        """Rotate GPU sparkline metric."""
        spark_cfg = get_gpu_sparkline_config(self.state.gpu_sparkline_preset)
        metric_names = [metric.display_label for metric in spark_cfg.metrics]
        if not metric_names:
            return

        with self.state._lock:
            self.state.gpu_sparkline_metric_idx = (
                self.state.gpu_sparkline_metric_idx + 1
            ) % len(metric_names)
            current_name = metric_names[self.state.gpu_sparkline_metric_idx]
            self.state.set_last_action(f"GPU Graph: {current_name}")

    def on_cycle_sparkline_preset(self, event: CycleSparklinePreset):
        """Cycle GPU sparkline preset (TUI tab only)."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "tui":
                return
            presets = list_gpu_sparkline_presets()
            if not presets:
                return
            if self.state.gpu_sparkline_mode != "sparkline":
                next_preset = presets[0]
                self.state.gpu_sparkline_mode = "sparkline"
            else:
                try:
                    current_idx = presets.index(self.state.gpu_sparkline_preset)
                except ValueError:
                    current_idx = 0
                next_idx = (current_idx + event.direction) % len(presets)
                next_preset = presets[next_idx]
            self.state.gpu_sparkline_preset = next_preset
            spark_cfg = get_gpu_sparkline_config(next_preset)
            if spark_cfg.metrics:
                self.state.gpu_sparkline_metric_idx %= len(spark_cfg.metrics)
            else:
                self.state.gpu_sparkline_metric_idx = 0
            preset_label = format_preset_label(next_preset, spark_cfg)
            self.state.set_last_action(f"Sparkline: {preset_label}")

    def on_cycle_sparkline_palette(self, event: CycleSparklinePalette):
        """Cycle GPU sparkline palette (TUI tab only)."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "tui":
                return
            palettes = list_gpu_sparkline_palettes()
            if not palettes:
                return
            if self.state.gpu_sparkline_mode != "palette":
                next_palette = palettes[0]
                self.state.gpu_sparkline_mode = "palette"
            else:
                try:
                    current_idx = palettes.index(self.state.gpu_sparkline_palette)
                except ValueError:
                    current_idx = 0
                next_idx = (current_idx + event.direction) % len(palettes)
                next_palette = palettes[next_idx]
            self.state.gpu_sparkline_palette = next_palette
            palette = get_gpu_sparkline_palette(next_palette)
            self.state.set_last_action(f"Palette: {palette.display_label}")

    def on_cycle_logs_page(self, event: CycleLogsPage):
        """Cycle page in Logs tab only."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "logs":
                return
        self.state.cycle_logs_page(event.direction)

    def on_job_started(self, event: JobStarted):
        # Track when first job starts
        from datetime import datetime
        if self.state.processing_start_time is None:
            self.state.processing_start_time = datetime.now()
        self.state.add_active_job(event.job)

    def on_job_completed(self, event: JobCompleted):
        output_size = 0
        if event.job.output_size_bytes is not None:
            output_size = event.job.output_size_bytes
        elif event.job.output_path and event.job.output_path.exists():
            output_size = event.job.output_path.stat().st_size

        # Calculate duration
        from datetime import datetime
        filename = event.job.source_file.path.name
        if filename in self.state.job_start_times:
            start_time = self.state.job_start_times[filename]
            event.job.duration_seconds = (datetime.now() - start_time).total_seconds()

        # Check if this is a min_ratio_skip (original file kept)
        if event.job.error_message and "kept original" in event.job.error_message:
            with self.state._lock:
                self.state.min_ratio_skip_count += 1

        self.state.add_completed_job(event.job, output_size)

    def on_job_failed(self, event: JobFailed):
        # Calculate duration
        from datetime import datetime
        from vbc.domain.models import JobStatus
        self.state.add_session_error(event.job, event.error_message)
        filename = event.job.source_file.path.name
        if filename in self.state.job_start_times:
            start_time = self.state.job_start_times[filename]
            event.job.duration_seconds = (datetime.now() - start_time).total_seconds()

        # Check if it's an AV1 skip
        if event.error_message and "Already encoded in AV1" in event.error_message:
            with self.state._lock:
                self.state.ignored_av1_count += 1
            # Don't add to failed jobs - just increment counter
            self.state.remove_active_job(event.job)
        # Check if it's a camera filter skip
        elif event.error_message and "Camera model" in event.error_message:
            with self.state._lock:
                self.state.cam_skipped_count += 1
            # Don't add to failed jobs - just increment counter
            self.state.remove_active_job(event.job)
        # Check if it's INTERRUPTED (Ctrl+C)
        elif event.job.status == JobStatus.INTERRUPTED:
            with self.state._lock:
                self.state.interrupted_count += 1
            # Add to recent jobs to show in LAST COMPLETED
            self.state.recent_jobs.appendleft(event.job)
            self.state.remove_active_job(event.job)
        else:
            self.state.add_failed_job(event.job)

    def on_hw_cap_exceeded(self, event: HardwareCapabilityExceeded):
        self.state.hw_cap_count += 1
        # Don't add to recent_jobs - hw_cap is only counted, not shown in LAST COMPLETED
        self.state.remove_active_job(event.job)

    def on_job_progress(self, event: JobProgressUpdated):
        with self.state._lock:
            # Find the active job by source filename (more robust than full path)
            target_name = event.job.source_file.path.name
            for job in self.state.active_jobs:
                if job.source_file.path.name == target_name:
                    job.progress_percent = event.progress_percent
                    break

    def on_queue_updated(self, event: QueueUpdated):
        pending_files = list(event.pending_files)
        with self.state._lock:
            # Store VideoFile objects (not just paths) to preserve metadata
            self.state.pending_files = pending_files
            dir_stats = list(self.state.io_input_dir_stats)

        if not dir_stats:
            return

        # In demo mode, keep mockup data instead of recalculating from actual files
        if self.demo_mode:
            return

        dir_paths = []
        for status, entry, _, _ in dir_stats:
            dir_paths.append((status, entry, Path(entry)))

        counts = {entry: [0, 0] for _, entry, _ in dir_paths}
        for vf in pending_files:
            for status, entry, dir_path in dir_paths:
                if status != STATUS_OK:
                    continue
                try:
                    vf.path.relative_to(dir_path)
                except ValueError:
                    continue
                counts[entry][0] += 1
                counts[entry][1] += vf.size_bytes
                break

        new_stats = []
        for status, entry, _ in dir_paths:
            if status != STATUS_OK:
                new_stats.append((status, entry, None, None))
                continue
            count, size_bytes = counts.get(entry, [0, 0])
            new_stats.append((status, entry, count, size_bytes))

        with self.state._lock:
            self.state.io_input_dir_stats = new_stats

    def on_action_message(self, event: ActionMessage):
        """Handle user action feedback messages (like old vbc.py)."""
        self.state.set_last_action(event.message)

    def on_processing_finished(self, event: ProcessingFinished):
        with self.state._lock:
            self.state.finished = True
            self.state.waiting_for_input = False

    def on_waiting_for_input(self, event: WaitingForInput):
        with self.state._lock:
            self.state.finished = False
            self.state.waiting_for_input = True

    # ── Dirs tab event handlers ────────────────────────────────────────────────

    def on_dirs_cursor_move(self, event: DirsCursorMove):
        """Move cursor in the Dirs tab directory list."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "dirs":
                return
            entries = self.state.dirs_get_all_entries()
            if not entries:
                return
            new_pos = self.state.dirs_cursor + event.direction
            self.state.dirs_cursor = max(0, min(new_pos, len(entries) - 1))

    def on_dirs_toggle_selected(self, event: DirsToggleSelected):
        """Toggle enabled/disabled state of the dir under cursor."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "dirs":
                return
            entries = self.state.dirs_get_all_entries()
            if not entries or self.state.dirs_cursor >= len(entries):
                return
            entry = entries[self.state.dirs_cursor]
            path, status = entry[0], entry[1]
            fs_status = entry[4] if len(entry) > 4 else None

            # Cannot toggle pending_add or pending_remove entries
            if status in ("pending_add", "pending_remove"):
                return

            if status in ("active", "pending_toggle_off"):
                # Toggle between active and pending-disable
                if status == "pending_toggle_off":
                    del self.state.dirs_pending_toggle[path]
                else:
                    self.state.dirs_pending_toggle[path] = False

            elif status in ("disabled", "pending_toggle_on"):
                # Block enabling if path doesn't exist or isn't accessible
                from pathlib import Path as _Path
                import os as _os
                p = _Path(path)
                if not p.exists():
                    self.state.dirs_error_msg = "Cannot enable: path does not exist"
                    return
                if not _os.access(p, _os.R_OK | _os.X_OK):
                    self.state.dirs_error_msg = "Cannot enable: no read access"
                    return
                self.state.dirs_error_msg = ""
                # Toggle between disabled and pending-enable
                if status == "pending_toggle_on":
                    del self.state.dirs_pending_toggle[path]
                else:
                    self.state.dirs_pending_toggle[path] = True

    def on_dirs_enter_add_mode(self, event: DirsEnterAddMode):
        """Enter add-path input mode."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "dirs":
                return
            self.state.dirs_input_mode = True
            self.state.dirs_input_buffer = ""

    def on_dirs_mark_delete(self, event: DirsMarkDelete):
        """Mark the dir under cursor for deletion (or unmark if already marked)."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "dirs":
                return
            entries = self.state.dirs_get_all_entries()
            if not entries or self.state.dirs_cursor >= len(entries):
                return
            path, status = entries[self.state.dirs_cursor][:2]

            if status == "pending_remove":
                # Unmark
                self.state.dirs_pending_remove.discard(path)
            elif status == "pending_add":
                # Cancel the pending add
                if path in self.state.dirs_pending_add:
                    self.state.dirs_pending_add.remove(path)
                # Cursor may be out of bounds after removal — clamp
                new_max = max(0, len(self.state.dirs_get_all_entries()) - 1)
                self.state.dirs_cursor = min(self.state.dirs_cursor, new_max)
            else:
                self.state.dirs_pending_remove.add(path)

    def on_dirs_input_char(self, event: DirsInputChar):
        """Append a character to (or backspace from) the add-path input buffer."""
        with self.state._lock:
            if not self.state.dirs_input_mode:
                return
            if event.char == '\x7f':
                self.state.dirs_input_buffer = self.state.dirs_input_buffer[:-1]
            else:
                self.state.dirs_input_buffer += event.char

    def on_dirs_confirm_add(self, event: DirsConfirmAdd):
        """Validate and stage the input buffer as a pending add."""
        with self.state._lock:
            if not self.state.dirs_input_mode:
                return
            path = self.state.dirs_input_buffer.strip()
            self.state.dirs_input_mode = False
            self.state.dirs_input_buffer = ""

            if not path:
                return

            # Check total directory limit
            total = (
                len(self.state.io_input_dir_stats)
                + len(self.state.dirs_disabled_entries)
                + len(self.state.dirs_pending_add)
            )
            if total >= DirsOverlay.MAX_DIRS:
                self.state.set_last_action(
                    f"Dirs limit reached ({DirsOverlay.MAX_DIRS} max). Remove a directory first."
                )
                return

            # Skip duplicates
            existing = (
                {e for _, e, _, _ in self.state.io_input_dir_stats}
                | set(self.state.dirs_disabled_entries)
                | set(self.state.dirs_pending_add)
            )
            if path in existing:
                self.state.set_last_action(f"Directory already in list: {path}")
                return

            self.state.dirs_pending_add.append(path)

    def on_dirs_cancel_input(self, event: DirsCancelInput):
        """Cancel add-path input mode."""
        with self.state._lock:
            self.state.dirs_input_mode = False
            self.state.dirs_input_buffer = ""

    def on_dirs_apply_changes(self, event: DirsApplyChanges):
        """Apply all pending Dirs changes, persist to YAML, and trigger re-scan."""
        with self.state._lock:
            current_active = [entry for _, entry, _, _ in self.state.io_input_dir_stats]
            current_disabled = list(self.state.dirs_disabled_entries)
            pending_toggle = dict(self.state.dirs_pending_toggle)
            pending_add = list(self.state.dirs_pending_add)
            pending_remove = set(self.state.dirs_pending_remove)

        # Compute new active list
        new_active: list = []
        for d in current_active:
            if d in pending_remove:
                continue
            if pending_toggle.get(d) is False:
                continue  # toggled off → goes to disabled
            new_active.append(d)
        for d in current_disabled:
            if d in pending_remove:
                continue
            if pending_toggle.get(d) is True:
                new_active.append(d)  # toggled on → becomes active
        for d in pending_add:
            if d not in pending_remove:
                new_active.append(d)

        # Compute new disabled list
        new_disabled: list = []
        for d in current_disabled:
            if d in pending_remove:
                continue
            if pending_toggle.get(d) is True:
                continue  # moved to active
            new_disabled.append(d)
        for d in current_active:
            if d in pending_remove:
                continue
            if pending_toggle.get(d) is False:
                new_disabled.append(d)  # toggled off → becomes disabled

        # Persist to YAML
        if self.config_path:
            try:
                from vbc.config.loader import save_dirs_config
                save_dirs_config(self.config_path, new_active, new_disabled)
            except Exception as exc:
                self.state.set_last_action(f"ERROR saving config: {exc}")
                return

        # Update state
        new_disabled_set = set(new_disabled)
        existing_active = {entry for _, entry, _, _ in self.state.io_input_dir_stats}
        with self.state._lock:
            self.state.dirs_disabled_entries = new_disabled
            # Remove disabled/removed dirs from io_input_dir_stats.
            self.state.io_input_dir_stats = [
                (label, entry, count, size)
                for label, entry, count, size in self.state.io_input_dir_stats
                if entry not in new_disabled_set and entry not in pending_remove
            ]
            # Add newly-activated dirs (were disabled, now active) to io_input_dir_stats.
            for d in new_active:
                if d not in existing_active:
                    from vbc.config.input_dirs import STATUS_OK
                    self.state.io_input_dir_stats.append((STATUS_OK, d, None, None))
            self.state.dirs_pending_toggle.clear()
            self.state.dirs_pending_add.clear()
            self.state.dirs_pending_remove.clear()
            self.state.dirs_input_mode = False
            self.state.dirs_input_buffer = ""

        n_active = len(new_active)
        n_disabled = len(new_disabled)
        self.state.set_last_action(
            f"Dirs saved: {n_active} active, {n_disabled} disabled"
        )
        self.bus.publish(InputDirsChanged(active_dirs=new_active))
        self.bus.publish(RefreshRequested())

