import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from vbc.pipeline.orchestrator import Orchestrator
from vbc.config.models import AppConfig, GeneralConfig
from vbc.domain.models import VideoFile, CompressionJob, JobStatus, VideoMetadata
from vbc.domain.events import JobFailed

def test_orchestrator_creates_err_file_on_failure(tmp_path):
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    video_file_path = input_dir / "failed.mp4"
    video_file_path.write_text("fake video content")

    vf = VideoFile(path=video_file_path, size_bytes=video_file_path.stat().st_size)

    config = AppConfig(general=GeneralConfig(threads=1, debug=False))

    mock_bus = MagicMock()
    mock_scanner = MagicMock()
    mock_scanner.scan.return_value = [vf]
    mock_exif = MagicMock()
    mock_exif.extract_exif_info.return_value = {}

    mock_ffprobe = MagicMock()
    # Must return valid dict, not MagicMock for nested values
    mock_ffprobe.get_stream_info.return_value = {
        'width': 1920,
        'height': 1080,
        'codec': 'h264',
        'fps': 30.0,
        'color_space': None,
        'duration': 10.0
    }

    mock_ffmpeg = MagicMock()

    # Simulate failure
    def compress_side_effect(job, config, use_gpu=False, **kwargs):
        job.status = JobStatus.FAILED
        job.error_message = "MOCK ERROR"
    mock_ffmpeg.compress.side_effect = compress_side_effect

    orchestrator = Orchestrator(
        config=config,
        event_bus=mock_bus,
        file_scanner=mock_scanner,
        exif_adapter=mock_exif,
        ffprobe_adapter=mock_ffprobe,
        ffmpeg_adapter=mock_ffmpeg
    )

    orchestrator.run(input_dir)

    # Check if .err file exists in output directory
    output_dir = tmp_path / "in_out"
    err_file = output_dir / "failed.err"  # Note: .err extension, not .mp4.err
    assert err_file.exists()
    assert err_file.read_text() == "MOCK ERROR"

def test_orchestrator_skips_existing_err_file(tmp_path):
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    video_file_path = input_dir / "skipped.mp4"
    video_file_path.write_text("fake video content")

    output_dir = tmp_path / "in_out"
    output_dir.mkdir()
    err_file = output_dir / "skipped.err"
    err_file.write_text("previous error")

    vf = VideoFile(path=video_file_path, size_bytes=video_file_path.stat().st_size)

    config = AppConfig(general=GeneralConfig(threads=1, clean_errors=False, debug=False))

    mock_bus = MagicMock()
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

    orchestrator = Orchestrator(
        config=config,
        event_bus=mock_bus,
        file_scanner=mock_scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=mock_ffprobe,
        ffmpeg_adapter=mock_ffmpeg
    )

    orchestrator.run(input_dir)

    # Verify ffmpeg.compress was NOT called
    mock_ffmpeg.compress.assert_not_called()

def test_orchestrator_retries_with_clean_errors(tmp_path):
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    video_file_path = input_dir / "retry.mp4"
    video_file_path.write_text("fake video content")

    output_dir = tmp_path / "in_out"
    output_dir.mkdir()
    err_file = output_dir / "retry.err"
    err_file.write_text("previous error")

    vf = VideoFile(path=video_file_path, size_bytes=video_file_path.stat().st_size)

    # clean_errors=True
    config = AppConfig(general=GeneralConfig(threads=1, clean_errors=True, debug=False))

    mock_bus = MagicMock()
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

    mock_exif = MagicMock()
    mock_exif.extract_exif_info.return_value = {}

    orchestrator = Orchestrator(
        config=config,
        event_bus=mock_bus,
        file_scanner=mock_scanner,
        exif_adapter=mock_exif,
        ffprobe_adapter=mock_ffprobe,
        ffmpeg_adapter=mock_ffmpeg
    )

    orchestrator.run(input_dir)

    # Verify ffmpeg.compress WAS called because we cleaned errors
    mock_ffmpeg.compress.assert_called_once()
    # And .err file should be gone (or overwritten if it fails again, but here it doesn't fail unless mock says so)
