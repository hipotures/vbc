import pytest
from pathlib import Path
from unittest.mock import MagicMock
from vbc.pipeline.orchestrator import Orchestrator
from vbc.config.models import AppConfig, GeneralConfig
from vbc.domain.models import VideoFile, VideoMetadata, JobStatus, CompressionJob

def test_skip_av1_logic():
    """Test that files already encoded in AV1 are skipped when skip_av1=True."""
    config = AppConfig(general=GeneralConfig(threads=1, skip_av1=True, debug=False))

    # File already AV1
    vf_av1 = VideoFile(
        path=Path("/tmp/test_av1/already_av1.mp4"),
        size_bytes=1000
    )

    # Mock scanner to return this file
    mock_scanner = MagicMock()
    mock_scanner.scan.return_value = [vf_av1]

    mock_ffprobe = MagicMock()
    # Return AV1 codec to trigger skip
    mock_ffprobe.get_stream_info.return_value = {
        'width': 1920,
        'height': 1080,
        'codec': 'av1',  # This will trigger skip_av1
        'fps': 30.0,
        'color_space': None,
        'duration': 10.0
    }

    mock_ffmpeg = MagicMock()
    mock_bus = MagicMock()

    orchestrator = Orchestrator(
        config=config,
        event_bus=mock_bus,
        file_scanner=mock_scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=mock_ffprobe,
        ffmpeg_adapter=mock_ffmpeg
    )

    orchestrator.run(Path("/tmp/test_av1"))

    # Verify ffmpeg.compress was NOT called
    mock_ffmpeg.compress.assert_not_called()

def test_min_compression_ratio_revert(tmp_path):
    """Test that original file is kept if compression savings < min_compression_ratio."""
    # ratio 0.1 means we need at least 10% savings
    config = AppConfig(general=GeneralConfig(threads=1, min_compression_ratio=0.1, debug=False))

    input_dir = tmp_path / "in"
    input_dir.mkdir()
    input_file = input_dir / "large.mp4"
    input_file.write_text("a" * 1000)

    # Expected output path by orchestrator
    output_dir = tmp_path / "in_out"
    output_file = output_dir / "large.mp4"

    vf = VideoFile(path=input_file, size_bytes=1000)

    mock_scanner = MagicMock()
    mock_scanner.scan.return_value = [vf]

    mock_ffprobe = MagicMock()
    mock_ffprobe.get_stream_info.return_value = {
        'width': 1920,
        'height': 1080,
        'codec': 'h264',
        'fps': 30.0,
        'color_space': None,
        'duration': 10.0
    }

    mock_ffmpeg = MagicMock()
    def compress_side_effect(job, config, **kwargs):
        job.status = JobStatus.COMPLETED
        # Create 'compressed' file that is actually too large
        job.output_path.parent.mkdir(parents=True, exist_ok=True)
        job.output_path.write_text("b" * 950) # 5% savings only
    mock_ffmpeg.compress.side_effect = compress_side_effect

    mock_exif = MagicMock()

    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=mock_scanner,
        exif_adapter=mock_exif,
        ffprobe_adapter=mock_ffprobe,
        ffmpeg_adapter=mock_ffmpeg
    )

    orchestrator.run(input_dir)

    # Original content should have been copied over (1000 bytes 'a')
    assert output_file.exists()
    assert output_file.stat().st_size == 1000
    assert output_file.read_text() == "a" * 1000


