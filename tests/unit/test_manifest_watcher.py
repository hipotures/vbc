import os
import sys
import threading
import time

import pytest
from inotify_simple import flags

from vbc.domain.events import InputDirsChanged, RefreshRequested
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.manifest_watcher import ManifestWatcher


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="inotify is available only on Linux",
)


def _watcher(tmp_path):
    bus = EventBus()
    refreshed = threading.Event()
    refresh_count = []

    def on_refresh(_event):
        refresh_count.append(1)
        refreshed.set()

    bus.subscribe(RefreshRequested, on_refresh)
    watcher = ManifestWatcher(bus, [tmp_path], [tmp_path])
    watcher._DEBOUNCE_SECONDS = 0.05
    watcher._READ_TIMEOUT_MS = 10
    return watcher, refreshed, refresh_count


def test_atomic_manifest_move_triggers_one_debounced_refresh(tmp_path):
    watcher, refreshed, refresh_count = _watcher(tmp_path)
    watcher.start()
    try:
        first_tmp = tmp_path / ".first.tmp"
        second_tmp = tmp_path / ".second.tmp"
        first_tmp.write_text("{}")
        second_tmp.write_text("{}")
        os.replace(first_tmp, tmp_path / "first.json")
        os.replace(second_tmp, tmp_path / "second.json")

        assert refreshed.wait(1.0)
        time.sleep(0.1)
        assert len(refresh_count) == 1
    finally:
        watcher.stop()


def test_tmp_close_is_ignored_but_direct_json_close_is_ready(tmp_path):
    watcher, refreshed, refresh_count = _watcher(tmp_path)
    watcher.start()
    try:
        (tmp_path / ".request.tmp").write_text("partial")
        assert not refreshed.wait(0.15)

        (tmp_path / "request.json").write_text("{}")
        assert refreshed.wait(1.0)
        assert len(refresh_count) == 1
    finally:
        watcher.stop()


def test_queue_overflow_forces_immediate_refresh(tmp_path):
    watcher, refreshed, refresh_count = _watcher(tmp_path)

    watcher._handle_event(-1, flags.Q_OVERFLOW, "")

    assert refreshed.is_set()
    assert len(refresh_count) == 1


def test_start_fails_for_missing_watched_directory(tmp_path):
    missing = tmp_path / "missing"
    watcher = ManifestWatcher(EventBus(), [missing], [missing])

    with pytest.raises(RuntimeError, match="Could not watch metadata directory"):
        watcher.start()


def test_active_directory_change_moves_the_watch(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    bus = EventBus()
    refreshed = threading.Event()
    bus.subscribe(RefreshRequested, lambda _event: refreshed.set())
    watcher = ManifestWatcher(bus, [first, second], [first])
    watcher._DEBOUNCE_SECONDS = 0.05
    watcher._READ_TIMEOUT_MS = 10
    watcher.start()
    try:
        bus.publish(InputDirsChanged(active_dirs=[str(second)]))
        deadline = time.monotonic() + 1.0
        while second not in watcher._path_to_wd and time.monotonic() < deadline:
            time.sleep(0.01)

        assert second in watcher._path_to_wd
        assert first not in watcher._path_to_wd
        (second / "request.json").write_text("{}")
        assert refreshed.wait(1.0)
    finally:
        watcher.stop()
