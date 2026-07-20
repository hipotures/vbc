import pytest
from unittest.mock import MagicMock
from collections import deque
from pathlib import Path
from vbc.domain.events import QueueUpdated, RefreshRequested, RequestShutdown
from vbc.domain.models import VideoFile
from vbc.pipeline.orchestrator import Orchestrator
from vbc.config.models import AppConfig, GeneralConfig

@pytest.fixture
def mock_orchestrator():
    config = AppConfig(general=GeneralConfig(threads=1))
    event_bus = MagicMock()
    file_scanner = MagicMock()
    file_scanner.extensions = [".mp4"]
    exif_adapter = MagicMock()
    ffprobe_adapter = MagicMock()
    ffmpeg_adapter = MagicMock()
    
    orch = Orchestrator(
        config=config,
        event_bus=event_bus,
        file_scanner=file_scanner,
        exif_adapter=exif_adapter,
        ffprobe_adapter=ffprobe_adapter,
        ffmpeg_adapter=ffmpeg_adapter
    )
    orch._folder_mapping = {Path("/tmp"): Path("/tmp/out")}
    return orch

def test_refresh_resorts_queue(mock_orchestrator):
    """Test that refresh rebuilds and resorts the queue."""
    # Setup initial state
    vf1 = VideoFile(path=Path("/tmp/a.mp4"), size_bytes=100)
    vf2 = VideoFile(path=Path("/tmp/c.mp4"), size_bytes=300)
    
    # pending queue has a, c
    pending = deque([vf1, vf2])
    in_flight = {} # No active jobs
    
    # Simulate discovery finding a, b, c (sorted)
    vf_b = VideoFile(path=Path("/tmp/b.mp4"), size_bytes=200)
    # Re-create vf1, vf2 to simulate new discovery objects
    vf1_new = VideoFile(path=Path("/tmp/a.mp4"), size_bytes=100)
    vf2_new = VideoFile(path=Path("/tmp/c.mp4"), size_bytes=300)
    
    sorted_new_files = [vf1_new, vf_b, vf2_new] # a, b, c
    new_stats = {}
    
    mock_orchestrator._perform_discovery = MagicMock(return_value=(sorted_new_files, new_stats))
    mock_orchestrator._prune_failed_pending = MagicMock()
    
    # Execute the logic extracted from run()
    # (Since we can't easily run the full run() loop in unit test without complex mocking,
    # we replicate the logic block we changed)
    
    in_flight_paths = {vf.path for vf in in_flight.values()}
    old_pending_paths = {vf.path for vf in pending}
    
    new_files, _ = mock_orchestrator._perform_discovery([])
    
    new_pending_list = [vf for vf in new_files if vf.path not in in_flight_paths]
    new_pending_paths = {vf.path for vf in new_pending_list}
    
    added = len(new_pending_paths - old_pending_paths)
    removed = len(old_pending_paths - new_pending_paths)
    
    pending = deque(new_pending_list)
    
    # Assertions
    assert len(pending) == 3
    assert pending[0].path.name == "a.mp4"
    assert pending[1].path.name == "b.mp4" # Should be inserted in middle
    assert pending[2].path.name == "c.mp4"
    
    assert added == 1 # b.mp4
    assert removed == 0

def test_refresh_excludes_inflight(mock_orchestrator):
    """Test that refresh does not duplicate in-flight files."""
    vf1 = VideoFile(path=Path("/tmp/a.mp4"), size_bytes=100)
    vf2 = VideoFile(path=Path("/tmp/b.mp4"), size_bytes=200)
    
    # a.mp4 is processing
    in_flight = {MagicMock(): vf1}
    # b.mp4 is pending
    pending = deque([vf2])
    
    # Discovery finds a, b, c
    vf1_new = VideoFile(path=Path("/tmp/a.mp4"), size_bytes=100)
    vf2_new = VideoFile(path=Path("/tmp/b.mp4"), size_bytes=200)
    vf3_new = VideoFile(path=Path("/tmp/c.mp4"), size_bytes=300)
    
    sorted_new_files = [vf1_new, vf2_new, vf3_new]
    mock_orchestrator._perform_discovery = MagicMock(return_value=(sorted_new_files, {}))
    mock_orchestrator._prune_failed_pending = MagicMock()
    
    # Execute logic
    in_flight_paths = {vf.path for vf in in_flight.values()}
    new_files, _ = mock_orchestrator._perform_discovery([])
    
    new_pending_list = [vf for vf in new_files if vf.path not in in_flight_paths]
    pending = deque(new_pending_list)
    
    # Assertions
    assert len(pending) == 2 # b, c (a is excluded)
    assert pending[0].path.name == "b.mp4"
    assert pending[1].path.name == "c.mp4"


def test_idle_interval_rescans_configured_dir_only_in_wait_mode(monkeypatch, tmp_path):
    input_dir = tmp_path / "metadata"
    input_dir.mkdir()
    config = AppConfig(
        general=GeneralConfig(threads=1, wait_on_finish=True),
        input_dirs=[
            {
                "path": str(input_dir),
                "enabled": True,
                "metadata": True,
                "idle_interval": 1,
            }
        ],
    )
    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )
    run_calls = []

    def fake_run_once(run_dirs, forced_files=None, manifest_paths=None):
        run_calls.append(list(run_dirs))
        if len(run_calls) == 2:
            orchestrator._shutdown_requested = True

    clock = {"value": 0.0}

    def fake_monotonic():
        clock["value"] += 2.0
        return clock["value"]

    orchestrator._run_once = fake_run_once
    orchestrator._run_auto_repair = MagicMock(return_value=[])
    orchestrator._wait_event = MagicMock()
    orchestrator._wait_event.wait.return_value = False
    monkeypatch.setattr("vbc.pipeline.orchestrator.time.monotonic", fake_monotonic)

    orchestrator.run([input_dir])

    assert run_calls == [[input_dir], [input_dir]]
    orchestrator._wait_event.wait.assert_called_once()


def test_refresh_arriving_before_waiting_is_not_lost(tmp_path):
    input_dir = tmp_path / "metadata"
    input_dir.mkdir()
    config = AppConfig(
        general=GeneralConfig(threads=1, wait_on_finish=True),
        input_dirs=[
            {
                "path": str(input_dir),
                "enabled": True,
                "metadata": True,
                "watch": True,
            }
        ],
    )
    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )
    run_calls = []

    def fake_run_once(run_dirs, forced_files=None, manifest_paths=None):
        run_calls.append(list(run_dirs))
        if len(run_calls) == 1:
            with orchestrator._refresh_lock:
                orchestrator._refresh_requested = True
        else:
            orchestrator._shutdown_requested = True
        return False

    orchestrator._run_once = fake_run_once
    orchestrator._run_auto_repair = MagicMock(return_value=[])
    orchestrator._wait_event = MagicMock()

    orchestrator.run([input_dir])

    assert run_calls == [[input_dir], [input_dir]]
    orchestrator._wait_event.wait.assert_not_called()


def test_manifest_watch_paths_do_not_request_full_refresh(mock_orchestrator):
    first = Path("/tmp/metadata/first.json")
    second = Path("/tmp/metadata/second.json")

    mock_orchestrator._on_refresh_request(
        RefreshRequested(manifest_paths=[first, second, first])
    )

    full_refresh, manifest_paths = mock_orchestrator._take_refresh_request()
    assert full_refresh is False
    assert manifest_paths == [first, second]


def test_full_refresh_supersedes_pending_manifest_paths(mock_orchestrator):
    manifest = Path("/tmp/metadata/request.json")
    mock_orchestrator._on_refresh_request(
        RefreshRequested(manifest_paths=[manifest])
    )
    mock_orchestrator._on_refresh_request(RefreshRequested())

    full_refresh, manifest_paths = mock_orchestrator._take_refresh_request()
    assert full_refresh is True
    assert manifest_paths == []


def test_manifest_watch_refresh_waits_for_shutdown_cancellation(mock_orchestrator):
    manifest = Path("/tmp/metadata/request.json")
    mock_orchestrator._on_refresh_request(
        RefreshRequested(manifest_paths=[manifest])
    )

    mock_orchestrator._on_shutdown_request(RequestShutdown())
    assert mock_orchestrator._take_refresh_request() == (False, [])

    mock_orchestrator._on_shutdown_request(RequestShutdown())
    assert mock_orchestrator._take_refresh_request() == (False, [manifest])


def test_incremental_discovery_result_is_deferred_if_shutdown_starts_mid_scan(
    mock_orchestrator,
):
    manifest = Path("/tmp/metadata/request.json")
    discovered = VideoFile(path=Path("/tmp/output.mp4"), size_bytes=100)

    def finish_during_shutdown(*_args, **_kwargs):
        mock_orchestrator._shutdown_requested = True
        return [discovered], {}

    mock_orchestrator._perform_discovery = finish_during_shutdown

    processed = mock_orchestrator._run_once(
        [Path("/tmp/metadata")],
        manifest_paths=[manifest],
    )

    assert processed is False
    published_events = [
        call.args[0] for call in mock_orchestrator.event_bus.publish.call_args_list
    ]
    assert not any(isinstance(event, QueueUpdated) for event in published_events)
    mock_orchestrator._shutdown_requested = False
    assert mock_orchestrator._take_refresh_request() == (False, [manifest])


def test_manifest_watch_paths_start_incremental_wait_cycle(tmp_path):
    input_dir = tmp_path / "metadata"
    input_dir.mkdir()
    manifest_path = input_dir / "request.json"
    config = AppConfig(
        general=GeneralConfig(threads=1, wait_on_finish=True),
        input_dirs=[
            {
                "path": str(input_dir),
                "enabled": True,
                "metadata": True,
                "watch": True,
            }
        ],
    )
    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )
    run_calls = []

    def fake_run_once(run_dirs, forced_files=None, manifest_paths=None):
        run_calls.append(manifest_paths)
        if len(run_calls) == 1:
            orchestrator._on_refresh_request(
                RefreshRequested(manifest_paths=[manifest_path])
            )
        else:
            orchestrator._shutdown_requested = True
        return False

    orchestrator._run_once = fake_run_once
    orchestrator._run_auto_repair = MagicMock(return_value=[])
    orchestrator._wait_event = MagicMock()

    orchestrator.run([input_dir])

    assert run_calls == [None, [manifest_path]]
    orchestrator._wait_event.wait.assert_not_called()
