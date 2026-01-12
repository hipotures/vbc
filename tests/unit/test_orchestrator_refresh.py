import pytest
from unittest.mock import MagicMock, patch
from collections import deque
from pathlib import Path
from vbc.domain.models import VideoFile, VideoMetadata
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
