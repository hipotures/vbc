import pytest
from pathlib import Path
from unittest.mock import MagicMock, call
from vbc.pipeline.orchestrator import Orchestrator
from vbc.config.models import AppConfig, GeneralConfig
from vbc.domain.models import VideoFile, CompressionJob, JobStatus, VideoMetadata
from vbc.domain.events import DiscoveryStarted, DiscoveryFinished, JobStarted, JobCompleted

def test_orchestrator_sequential_flow():
    """Test orchestrator executes pipeline stages in correct order."""
    # Mock dependencies
    mock_event_bus = MagicMock()
    mock_file_scanner = MagicMock()
    mock_exif = MagicMock()
    mock_ffprobe = MagicMock()
    mock_ffmpeg = MagicMock()

    # Setup test data
    root_dir = Path("/tmp/test_videos")
    test_file = VideoFile(path=root_dir / "test.mp4", size_bytes=1000)
    mock_file_scanner.scan.return_value = [test_file]

    # Mock ffprobe to return valid stream info
    mock_ffprobe.get_stream_info.return_value = {
        'width': 1920,
        'height': 1080,
        'codec': 'h264',
        'fps': 30.0,
        'color_space': None,
        'duration': 10.0
    }

    # Mock compression side effect to update job status
    def compress_side_effect(job, config, **kwargs):
        job.status = JobStatus.COMPLETED
    mock_ffmpeg.compress.side_effect = compress_side_effect

    # Initialize orchestrator
    config = AppConfig(general=GeneralConfig(threads=1, debug=False))
    orchestrator = Orchestrator(
        config=config,
        event_bus=mock_event_bus,
        file_scanner=mock_file_scanner,
        exif_adapter=mock_exif,
        ffprobe_adapter=mock_ffprobe,
        ffmpeg_adapter=mock_ffmpeg
    )

    # Run
    orchestrator.run(root_dir)

    # Verify sequence
    # 1. Discovery
    assert mock_event_bus.publish.call_args_list[0][0][0].__class__ == DiscoveryStarted
    mock_file_scanner.scan.assert_called_once_with(root_dir)

    # 2. FFprobe called for stream info
    mock_ffprobe.get_stream_info.assert_called()

    # 3. Compression
    mock_ffmpeg.compress.assert_called_once()
    job_arg = mock_ffmpeg.compress.call_args[0][0]
    assert isinstance(job_arg, CompressionJob)
    assert job_arg.source_file.path == test_file.path

    # 4. Events
    # Check for JobStarted and JobCompleted (or whatever events are emitted)
    event_types = [call[0][0].__class__ for call in mock_event_bus.publish.call_args_list]
    assert DiscoveryStarted in event_types
    assert DiscoveryFinished in event_types
    assert JobStarted in event_types
    assert JobCompleted in event_types
