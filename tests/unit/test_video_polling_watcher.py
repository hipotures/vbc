import threading

import pytest

from vbc.domain.events import InputDirsChanged, RefreshRequested
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.file_scanner import FileScanner
from vbc.infrastructure.polling_watcher import PollingWatcher


def _watcher(tmp_path, active_dirs=None):
    bus = EventBus()
    refreshed = threading.Event()
    refresh_events = []

    def on_refresh(event):
        refresh_events.append(event)
        refreshed.set()

    bus.subscribe(RefreshRequested, on_refresh)
    scanner = FileScanner([".mp4"], min_size_bytes=0)
    watcher = PollingWatcher(
        event_bus=bus,
        scan_paths=lambda directory: (
            video.path for video in scanner.scan(directory)
        ),
        watchable_dirs=[tmp_path],
        active_dirs=[tmp_path] if active_dirs is None else active_dirs,
        poll_interval_seconds=0.01,
    )
    return bus, watcher, refreshed, refresh_events


def test_polling_detects_renamed_configured_extension_and_ignores_tmp(tmp_path):
    _bus, watcher, _refreshed, refresh_events = _watcher(tmp_path)
    assert watcher._sync_directories() is False

    temporary = tmp_path / "recording.tmp"
    temporary.write_bytes(b"video")
    watcher._poll_once()
    assert refresh_events == []

    temporary.rename(tmp_path / "recording.mp4")
    watcher._poll_once()

    assert len(refresh_events) == 1
    assert refresh_events[0].manifest_paths == []


def test_polling_establishes_baseline_without_refreshing_existing_files(tmp_path):
    (tmp_path / "existing.mp4").write_bytes(b"video")
    _bus, watcher, _refreshed, _refresh_events = _watcher(tmp_path)

    assert watcher._sync_directories() is False


def test_metadata_polling_ignores_tmp_then_detects_final_json(tmp_path):
    bus = EventBus()
    refresh_events = []
    bus.subscribe(RefreshRequested, refresh_events.append)
    watcher = PollingWatcher(
        event_bus=bus,
        scan_paths=lambda directory: directory.rglob("*.json"),
        watchable_dirs=[tmp_path],
        active_dirs=[tmp_path],
    )
    assert watcher._sync_directories() is False

    temporary = tmp_path / "request.tmp"
    temporary.write_text("{}")
    watcher._poll_once()
    assert refresh_events == []

    temporary.rename(tmp_path / "request.json")
    watcher._poll_once()
    assert len(refresh_events) == 1
    assert refresh_events[0].manifest_paths == []


def test_polling_follows_active_directory_changes(tmp_path):
    bus, watcher, _refreshed, _refresh_events = _watcher(tmp_path, active_dirs=[])
    assert watcher._sync_directories() is False

    bus.publish(InputDirsChanged(active_dirs=[str(tmp_path)]))
    assert watcher._sync_directories() is False
    (tmp_path / "new.mp4").write_bytes(b"video")

    assert watcher._sync_directories() is True


def test_polling_thread_publishes_refresh_and_stops(tmp_path):
    _bus, watcher, refreshed, refresh_events = _watcher(tmp_path)
    watcher.start()
    try:
        temporary = tmp_path / "recording.tmp"
        temporary.write_bytes(b"video")
        assert not refreshed.wait(0.05)

        temporary.rename(tmp_path / "recording.mp4")
        assert refreshed.wait(1.0)
        assert len(refresh_events) == 1
        assert refresh_events[0].manifest_paths == []
    finally:
        watcher.stop()

    assert watcher._thread is None


def test_polling_rejects_non_positive_interval(tmp_path):
    with pytest.raises(ValueError, match="greater than zero"):
        PollingWatcher(
            event_bus=EventBus(),
            scan_paths=lambda directory: directory.rglob("*.mp4"),
            watchable_dirs=[tmp_path],
            active_dirs=[tmp_path],
            poll_interval_seconds=0,
        )
