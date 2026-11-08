"""Polling watcher for filesystems without reliable change notifications."""

import logging
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from vbc.domain.events import InputDirsChanged, RefreshRequested
from vbc.infrastructure.event_bus import EventBus


class PollingWatcher:
    """Request a full refresh when polling discovers new eligible paths."""

    def __init__(
        self,
        event_bus: EventBus,
        scan_paths: Callable[[Path], Iterable[Path]],
        watchable_dirs: Iterable[Path],
        active_dirs: Iterable[Path],
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        self.event_bus = event_bus
        self.scan_paths = scan_paths
        self.logger = logging.getLogger(__name__)
        self.poll_interval_seconds = poll_interval_seconds
        self._watchable_dirs = {Path(path) for path in watchable_dirs}
        self._desired_dirs = {
            Path(path) for path in active_dirs if Path(path) in self._watchable_dirs
        }
        self._desired_lock = threading.Lock()
        self._known_paths: dict[Path, set[Path]] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.event_bus.subscribe(InputDirsChanged, self._on_input_dirs_changed)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._sync_directories()
        self._thread = threading.Thread(
            target=self._run,
            name="vbc-polling-watcher",
            daemon=True,
        )
        self._thread.start()
        self.logger.info(
            "Polling watcher started for %s directories",
            len(self._known_paths),
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.logger.info("Polling watcher stopped")

    def _on_input_dirs_changed(self, event: InputDirsChanged) -> None:
        active_dirs = {Path(path) for path in event.active_dirs}
        with self._desired_lock:
            self._desired_dirs = active_dirs & self._watchable_dirs

    def _scan(self, directory: Path) -> set[Path]:
        return set(self.scan_paths(directory))

    def _sync_directories(self) -> bool:
        with self._desired_lock:
            desired_dirs = set(self._desired_dirs)

        for directory in set(self._known_paths) - desired_dirs:
            del self._known_paths[directory]

        discovered_new_path = False
        for directory in sorted(desired_dirs, key=str):
            current_paths = self._scan(directory)
            previous_paths = self._known_paths.get(directory)
            if previous_paths is not None and current_paths - previous_paths:
                discovered_new_path = True
            self._known_paths[directory] = current_paths
        return discovered_new_path

    def _poll_once(self) -> None:
        if self._sync_directories():
            self.logger.info("Polling watcher detected new input")
            self.event_bus.publish(RefreshRequested())

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_interval_seconds):
            self._poll_once()
