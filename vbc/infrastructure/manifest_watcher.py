"""Linux inotify watcher for completed metadata manifests."""

import logging
import threading
import time
from pathlib import Path
from typing import Iterable

from inotify_simple import INotify, flags

from vbc.domain.events import ActionMessage, InputDirsChanged, RefreshRequested
from vbc.infrastructure.event_bus import EventBus


class ManifestWatcher:
    """Turn completed JSON filesystem events into incremental queue updates."""

    _READ_TIMEOUT_MS = 100
    _DEBOUNCE_SECONDS = 1.0
    _READY_MASK = flags.CLOSE_WRITE | flags.MOVED_TO
    _WATCH_MASK = (
        _READY_MASK
        | flags.DELETE_SELF
        | flags.MOVE_SELF
        | flags.UNMOUNT
    )

    def __init__(
        self,
        event_bus: EventBus,
        watchable_dirs: Iterable[Path],
        active_dirs: Iterable[Path],
    ) -> None:
        self.event_bus = event_bus
        self.logger = logging.getLogger(__name__)
        self._watchable_dirs = {Path(path) for path in watchable_dirs}
        self._desired_dirs = {
            Path(path) for path in active_dirs if Path(path) in self._watchable_dirs
        }
        self._desired_lock = threading.Lock()
        self._inotify: INotify | None = None
        self._wd_to_path: dict[int, Path] = {}
        self._path_to_wd: dict[Path, int] = {}
        self._failed_dirs: set[Path] = set()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._refresh_deadline: float | None = None
        self._ready_paths: set[Path] = set()
        self.event_bus.subscribe(InputDirsChanged, self._on_input_dirs_changed)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._inotify = INotify(nonblocking=True)
        try:
            for directory in sorted(self._desired_dirs, key=str):
                self._add_watch(directory, fail_startup=True)
        except Exception:
            self._inotify.close()
            self._inotify = None
            raise
        self._thread = threading.Thread(
            target=self._run,
            name="vbc-manifest-watcher",
            daemon=True,
        )
        self._thread.start()
        self.logger.info(
            "Manifest watcher started for %s directories", len(self._path_to_wd)
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._inotify is not None:
            self._inotify.close()
            self._inotify = None
        self.logger.info("Manifest watcher stopped")

    def _on_input_dirs_changed(self, event: InputDirsChanged) -> None:
        active_dirs = {Path(path) for path in event.active_dirs}
        with self._desired_lock:
            new_desired = active_dirs & self._watchable_dirs
            newly_enabled = new_desired - self._desired_dirs
            self._desired_dirs = new_desired
            self._failed_dirs.difference_update(newly_enabled)

    def _add_watch(self, directory: Path, *, fail_startup: bool) -> None:
        if self._inotify is None:
            return
        try:
            wd = self._inotify.add_watch(str(directory), self._WATCH_MASK)
        except OSError as exc:
            if fail_startup:
                raise RuntimeError(
                    f"Could not watch metadata directory {directory}: {exc}"
                ) from exc
            self._failed_dirs.add(directory)
            self._alert(f"Manifest watch unavailable for {directory}: {exc}")
            return
        self._path_to_wd[directory] = wd
        self._wd_to_path[wd] = directory
        self.logger.info("Manifest watch enabled: %s", directory)

    def _sync_watches(self) -> None:
        if self._inotify is None:
            return
        with self._desired_lock:
            desired_dirs = set(self._desired_dirs)

        for directory in list(self._path_to_wd):
            if directory in desired_dirs:
                continue
            wd = self._path_to_wd.pop(directory)
            self._wd_to_path.pop(wd, None)
            try:
                self._inotify.rm_watch(wd)
            except OSError:
                pass

        for directory in sorted(desired_dirs - set(self._path_to_wd), key=str):
            if directory in self._failed_dirs:
                continue
            self._add_watch(directory, fail_startup=False)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._sync_watches()
            if self._inotify is None:
                return
            try:
                events = self._inotify.read(timeout=self._READ_TIMEOUT_MS)
            except (OSError, ValueError) as exc:
                if self._stop_event.is_set():
                    return
                self._alert(f"Manifest watcher stopped after read error: {exc}")
                return

            for event in events:
                self._handle_event(event.wd, event.mask, event.name)

            if (
                self._refresh_deadline is not None
                and time.monotonic() >= self._refresh_deadline
            ):
                self._refresh_deadline = None
                ready_paths = sorted(self._ready_paths, key=str)
                self._ready_paths.clear()
                if ready_paths:
                    self.event_bus.publish(
                        RefreshRequested(manifest_paths=ready_paths)
                    )

    def _handle_event(self, wd: int, mask: int, name: str) -> None:
        if mask & flags.Q_OVERFLOW:
            self._refresh_deadline = None
            self._ready_paths.clear()
            self._alert("Manifest watcher queue overflow; forcing full refresh")
            self.event_bus.publish(RefreshRequested())
            return

        directory = self._wd_to_path.get(wd)
        if directory is None:
            return

        if mask & (flags.DELETE_SELF | flags.MOVE_SELF | flags.UNMOUNT | flags.IGNORED):
            self._path_to_wd.pop(directory, None)
            self._wd_to_path.pop(wd, None)
            self._failed_dirs.add(directory)
            self._alert(f"Manifest watch lost for {directory}")
            return

        if not (mask & self._READY_MASK):
            return
        if not name or not name.endswith(".json"):
            return

        manifest_path = directory / name
        self.logger.info("Manifest ready event: %s", manifest_path)
        self._ready_paths.add(manifest_path)
        self._refresh_deadline = time.monotonic() + self._DEBOUNCE_SECONDS

    def _alert(self, message: str) -> None:
        self.logger.warning(message)
        self.event_bus.publish(ActionMessage(message=message))
