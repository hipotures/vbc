"""TUI controller: keyboard/view events -> client view state or API commands.

Port of vbc_legacy/ui/manager.py. View handlers (overlay tabs, dim level,
sparkline preset/palette/metric, logs page, Dirs editor) are unchanged and
act on ``ClientState``. Business handlers are gone: counters come from the
server. Command keys call the API on a worker thread so a slow network never
blocks the keyboard; Ctrl+C detaches the client (the server keeps running).
"""

import concurrent.futures
from pathlib import Path
from typing import Callable, Optional

from vbc import contracts as dto
from vbc.clients.api_client import ApiClient, ApiError, ApiUnavailable
from vbc.clients.tui.bus import EventBus
from vbc.clients.tui.state import MAX_INPUT_DIR_LEN, ClientState
from vbc.clients.tui.events import (
    ActionMessage, InterruptRequested, RefreshRequested, RequestShutdown, ThreadControlEvent,
    DirsCursorMove, DirsSwapSelected, DirsToggleSelected, DirsEnterAddMode, DirsMarkDelete,
    DirsInputChar, DirsConfirmAdd, DirsCancelInput, DirsApplyChanges,
)
from vbc.clients.tui.gpu_sparkline import (
    format_preset_label,
    get_gpu_sparkline_config,
    get_gpu_sparkline_palette,
    list_gpu_sparkline_palettes,
    list_gpu_sparkline_presets,
)
from vbc.clients.tui.keyboard import (
    ToggleOverlayTab, CycleOverlayTab, CloseOverlay, CycleOverlayDim, RotateGpuMetric,
    CycleSparklinePreset, CycleSparklinePalette, CycleLogsPage,
)
from vbc.clients.tui.modern_overlays import DirsOverlay


class TuiController:
    """Subscribes to the client-local bus; updates ClientState or calls the API."""

    def __init__(self, bus: EventBus, state: ClientState, client: ApiClient,
                 on_detach: Callable[[], None], on_changed: Callable[[], None] = lambda: None):
        self.bus = bus
        self.state = state
        self.client = client
        self.on_detach = on_detach
        self.on_changed = on_changed  # ask the poller to refetch now
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="vbc-tui-cmd")
        self._setup_subscriptions()

    def close(self) -> None:
        self._worker.shutdown(wait=False, cancel_futures=True)

    def _setup_subscriptions(self):
        self.bus.subscribe(ThreadControlEvent, self.on_thread_control)
        self.bus.subscribe(RequestShutdown, self.on_shutdown_request)
        self.bus.subscribe(RefreshRequested, self.on_refresh_request)
        self.bus.subscribe(InterruptRequested, self.on_interrupt_request)
        self.bus.subscribe(ActionMessage, self.on_action_message)
        self.bus.subscribe(ToggleOverlayTab, self.on_toggle_overlay_tab)
        self.bus.subscribe(CycleOverlayTab, self.on_cycle_overlay_tab)
        self.bus.subscribe(CloseOverlay, self.on_close_overlay)
        self.bus.subscribe(CycleOverlayDim, self.on_cycle_overlay_dim)
        self.bus.subscribe(RotateGpuMetric, self.on_rotate_gpu_metric)
        self.bus.subscribe(CycleSparklinePreset, self.on_cycle_sparkline_preset)
        self.bus.subscribe(CycleSparklinePalette, self.on_cycle_sparkline_palette)
        self.bus.subscribe(CycleLogsPage, self.on_cycle_logs_page)
        # Dirs tab events
        self.bus.subscribe(DirsCursorMove, self.on_dirs_cursor_move)
        self.bus.subscribe(DirsSwapSelected, self.on_dirs_swap_selected)
        self.bus.subscribe(DirsToggleSelected, self.on_dirs_toggle_selected)
        self.bus.subscribe(DirsEnterAddMode, self.on_dirs_enter_add_mode)
        self.bus.subscribe(DirsMarkDelete, self.on_dirs_mark_delete)
        self.bus.subscribe(DirsInputChar, self.on_dirs_input_char)
        self.bus.subscribe(DirsConfirmAdd, self.on_dirs_confirm_add)
        self.bus.subscribe(DirsCancelInput, self.on_dirs_cancel_input)
        self.bus.subscribe(DirsApplyChanges, self.on_dirs_apply_changes)

    # ── commands (API) ──────────────────────────────────────────────────────

    def _command(self, label: str, call: Callable[[], Optional[str]]) -> None:
        def run() -> None:
            try:
                message = call()
            except ApiUnavailable as exc:
                self.state.set_last_action(f"{label}: server unreachable")
                self.state.set_disconnected(str(exc))
                return
            except ApiError as exc:
                self.state.set_last_action(f"{label}: {exc}")
                return
            if message:
                self.state.set_last_action(message)
            self.on_changed()

        self._worker.submit(run)

    def on_thread_control(self, event: ThreadControlEvent):
        with self.state._lock:
            if self.state.shutdown_requested:
                return
            target = self.state.current_threads + event.change
        self._command("Threads", lambda: (self.client.set_threads(target), None)[1])

    def on_shutdown_request(self, event: RequestShutdown):
        with self.state._lock:
            pending = self.state.shutdown_requested
        if pending:
            self._command("Shutdown", lambda: (self.client.cancel_shutdown(), "SHUTDOWN cancelled")[1])
        else:
            self._command("Shutdown", lambda: (self.client.shutdown("graceful"),
                                               "SHUTDOWN requested (press S to cancel)")[1])

    def on_refresh_request(self, event: RefreshRequested):
        self._command("Refresh", lambda: (self.client.refresh(), None)[1])

    def on_interrupt_request(self, event: InterruptRequested):
        """Ctrl+C: detach this client; the server keeps running."""
        self.on_detach()

    def on_action_message(self, event: ActionMessage):
        self.state.set_last_action(event.message)

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

    # ── Dirs tab event handlers ────────────────────────────────────────────────

    def on_dirs_cursor_move(self, event: DirsCursorMove):
        """Move cursor in the Dirs tab directory list."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "dirs":
                return
            entries = self.state.dirs_get_all_entries()
            if not entries:
                return
            self.state.dirs_error_msg = ""
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
            self.state.dirs_error_msg = ""

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
                # Block enabling if the server reports the path missing / unreadable
                exists, readable = self.state.dirs_status.get(path, (True, True))
                if not exists:
                    self.state.dirs_error_msg = "Cannot enable: path does not exist on the server"
                    return
                if not readable:
                    self.state.dirs_error_msg = "Cannot enable: no read access on the server"
                    return
                self.state.dirs_error_msg = ""
                # Toggle between disabled and pending-enable
                if status == "pending_toggle_on":
                    del self.state.dirs_pending_toggle[path]
                else:
                    self.state.dirs_pending_toggle[path] = True

    def on_dirs_swap_selected(self, event: DirsSwapSelected):
        """Swap row under cursor with adjacent row in selected direction."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "dirs":
                return
            if self.state.dirs_input_mode:
                return
            entries = self.state.dirs_get_all_entries()
            if not entries or self.state.dirs_cursor >= len(entries):
                return

            src_idx = self.state.dirs_cursor
            dst_idx = src_idx + event.direction
            if dst_idx < 0 or dst_idx >= len(entries):
                return

            ordered_paths = [entry[0] for entry in entries]
            ordered_paths[src_idx], ordered_paths[dst_idx] = ordered_paths[dst_idx], ordered_paths[src_idx]
            self.state.dirs_set_pending_order(ordered_paths)
            self.state.dirs_cursor = dst_idx

    def on_dirs_enter_add_mode(self, event: DirsEnterAddMode):
        """Enter add-path input mode."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "dirs":
                return
            self.state.dirs_input_mode = True
            self.state.dirs_input_buffer = ""
            self.state.dirs_error_msg = ""

    def on_dirs_mark_delete(self, event: DirsMarkDelete):
        """Mark the dir under cursor for deletion (or unmark if already marked)."""
        with self.state._lock:
            if not self.state.show_overlay or self.state.active_tab != "dirs":
                return
            entries = self.state.dirs_get_all_entries()
            if not entries or self.state.dirs_cursor >= len(entries):
                return
            path, status = entries[self.state.dirs_cursor][:2]
            self.state.dirs_error_msg = ""

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
            elif len(self.state.dirs_input_buffer) < MAX_INPUT_DIR_LEN:
                self.state.dirs_input_buffer += event.char
            else:
                self.state.dirs_error_msg = f"Path is limited to {MAX_INPUT_DIR_LEN} characters"
                return
            self.state.dirs_error_msg = ""

    def on_dirs_confirm_add(self, event: DirsConfirmAdd):
        """Validate and stage the input buffer as a pending add."""
        with self.state._lock:
            if not self.state.dirs_input_mode:
                return
            path = self.state.dirs_input_buffer.strip()

            if not path:
                self.state.dirs_error_msg = "Enter a directory path"
                return

            if len(path) > MAX_INPUT_DIR_LEN:
                self.state.dirs_error_msg = f"Path is too long ({MAX_INPUT_DIR_LEN} characters max)"
                return

            # Check total directory limit
            total = len(self.state.dirs_config_entries) + len(self.state.dirs_pending_add)
            if total >= DirsOverlay.MAX_DIRS:
                self.state.dirs_error_msg = (
                    f"Directory limit reached ({DirsOverlay.MAX_DIRS} max); remove one first"
                )
                return

            # Skip duplicates
            existing = {entry for entry, _ in self.state.dirs_config_entries} | set(self.state.dirs_pending_add)
            if path in existing:
                self.state.dirs_error_msg = "Directory is already in the list"
                return

            if not Path(path).is_absolute():
                self.state.dirs_error_msg = "Use an absolute path on the server"
                return
            # Existence is checked by the server when the list is saved (422 shown here).

            self.state.dirs_pending_add.append(path)
            self.state.dirs_input_mode = False
            self.state.dirs_input_buffer = ""
            self.state.dirs_error_msg = ""

    def on_dirs_cancel_input(self, event: DirsCancelInput):
        """Cancel add-path input mode."""
        with self.state._lock:
            self.state.dirs_input_mode = False
            self.state.dirs_input_buffer = ""
            self.state.dirs_error_msg = ""

    def on_dirs_apply_changes(self, event: DirsApplyChanges):
        """Send the whole staged list to the server (PUT /dirs with If-Match)."""
        with self.state._lock:
            current_entries = list(self.state.dirs_config_entries)
            pending_toggle = dict(self.state.dirs_pending_toggle)
            pending_add = list(self.state.dirs_pending_add)
            pending_remove = set(self.state.dirs_pending_remove)
            effective_order = self.state.dirs_effective_order()
            dirs_rev = self.state.dirs_rev

        enabled_by_path = {path: enabled for path, enabled in current_entries}
        pending_add_set = set(pending_add)
        new_entries: list = []
        seen_paths: set[str] = set()
        for path in effective_order:
            if path in seen_paths or path in pending_remove:
                continue
            seen_paths.add(path)
            if path in enabled_by_path:
                new_entries.append((path, pending_toggle.get(path, enabled_by_path[path])))
            elif path in pending_add_set:
                new_entries.append((path, True))
        for path, enabled in current_entries:
            if path not in seen_paths and path not in pending_remove:
                seen_paths.add(path)
                new_entries.append((path, pending_toggle.get(path, enabled)))
        for path in pending_add:
            if path not in seen_paths and path not in pending_remove:
                seen_paths.add(path)
                new_entries.append((path, True))

        entries = [dto.DirEntry(path=path, enabled=enabled) for path, enabled in new_entries]

        def save() -> None:
            try:
                saved = self.client.put_dirs(entries, dirs_rev)
            except ApiError as exc:
                problem = exc.problem
                if exc.status == 412:
                    text = "Directories changed on the server; reopen the tab and retry"
                elif problem is not None and problem.errors:
                    text = problem.errors[0] if len(problem.errors) == 1 else f"{problem.errors[0]} (+{len(problem.errors) - 1} more)"
                else:
                    text = str(exc)
                with self.state._lock:
                    self.state.dirs_error_msg = text
                return
            except ApiUnavailable as exc:
                with self.state._lock:
                    self.state.dirs_error_msg = "Server unreachable; changes not saved"
                self.state.set_disconnected(str(exc))
                return
            self.state.apply_dirs(saved)
            with self.state._lock:
                self.state.dirs_pending_toggle.clear()
                self.state.dirs_pending_add.clear()
                self.state.dirs_pending_remove.clear()
                self.state.dirs_pending_order.clear()
                self.state.dirs_input_mode = False
                self.state.dirs_input_buffer = ""
                self.state.dirs_error_msg = ""
            n_active = sum(1 for e in entries if e.enabled)
            self.state.set_last_action(f"Dirs saved: {n_active} active, {len(entries) - n_active} disabled")
            self.on_changed()

        self._worker.submit(save)
