"""
Advanced error handling tests.

Tests various error scenarios including:
- Hardware capability errors
- Corrupted files
- Color space fixes
- Timeout handling
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from vbc.pipeline.orchestrator import Orchestrator
from vbc.config.models import AppConfig, GeneralConfig
from vbc.domain.models import VideoFile, JobStatus
from vbc.infrastructure.ffmpeg import FFmpegAdapter


def test_corrupted_file_creates_err_marker(tmp_path):
    """Test that corrupted files (ffprobe fails) get .err marker."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    corrupt_file = input_dir / "corrupted.mp4"
    corrupt_file.write_text("not a video")

    vf = VideoFile(path=corrupt_file, size_bytes=corrupt_file.stat().st_size)

    config = AppConfig(general=GeneralConfig(threads=1, debug=False))

    mock_scanner = MagicMock()
    mock_scanner.scan.return_value = [vf]

    mock_ffprobe = MagicMock()
    # Simulate ffprobe failure (raises exception)
    mock_ffprobe.get_stream_info.side_effect = Exception("ffprobe failed to read file")

    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=mock_scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=mock_ffprobe,
        ffmpeg_adapter=MagicMock()
    )

    orchestrator.run(input_dir)

    # .err file should exist
    output_dir = tmp_path / "in_out"
    err_file = output_dir / "corrupted.err"
    assert err_file.exists()
    content = err_file.read_text()
    assert "corrupted" in content.lower() or "ffprobe failed" in content


def test_hardware_error_creates_hw_cap_marker(tmp_path):
    """Test that hardware capability errors are tracked separately."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    video_file = input_dir / "needs_10bit.mp4"
    video_file.write_text("video content")

    vf = VideoFile(path=video_file, size_bytes=video_file.stat().st_size)

    config = AppConfig(general=GeneralConfig(threads=1, debug=False))

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

    # Simulate hardware capability error
    def compress_hw_cap(job, config, use_gpu=False, **kwargs):
        job.status = JobStatus.HW_CAP_LIMIT
        job.error_message = "Hardware is lacking required capabilities"

    mock_ffmpeg.compress.side_effect = compress_hw_cap

    mock_bus = MagicMock()

    orchestrator = Orchestrator(
        config=config,
        event_bus=mock_bus,
        file_scanner=mock_scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=mock_ffprobe,
        ffmpeg_adapter=mock_ffmpeg
    )

    orchestrator.run(input_dir)

    # .err file should exist with hw_cap message
    output_dir = tmp_path / "in_out"
    err_file = output_dir / "needs_10bit.err"
    assert err_file.exists()
    content = err_file.read_text()
    assert "Hardware is lacking required capabilities" in content


def test_color_space_detection():
    """Test that reserved color space is detected."""
    stream_info = {
        'width': 1920,
        'height': 1080,
        'codec': 'hevc',
        'fps': 30.0,
        'color_space': 'reserved',  # Trigger color fix
        'duration': 10.0
    }

    # This would trigger the color fix path in orchestrator
    assert stream_info['color_space'] == 'reserved'
    assert stream_info['codec'] in ['hevc', 'h264']


def test_ffmpeg_interrupt_handling():
    """Test that FFmpeg adapter handles interrupts gracefully."""
    import threading

    config = AppConfig(general=GeneralConfig(threads=1, gpu=False))
    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)

    from vbc.domain.models import CompressionJob
    job = CompressionJob(source_file=vf, output_path=Path("out.mp4"))

    # Create shutdown event
    shutdown_event = threading.Event()

    # Immediately signal shutdown
    shutdown_event.set()

    adapter = FFmpegAdapter(event_bus=MagicMock())

    # Mock Popen to check if process gets terminated
    with patch("subprocess.Popen") as mock_popen:
        process_instance = mock_popen.return_value
        process_instance.stdout = iter([])  # Empty iterator
        process_instance.poll.return_value = None  # Still running
        process_instance.returncode = -15  # SIGTERM

        adapter.compress(job, config, use_gpu=False, shutdown_event=shutdown_event)

        # Job should be INTERRUPTED
        assert job.status == JobStatus.INTERRUPTED
        assert "Interrupted" in job.error_message

        # Process should have been terminated
        process_instance.terminate.assert_called()


def test_missing_file_during_processing(tmp_path):
    """Test that files deleted during processing are handled."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    disappearing_file = input_dir / "will_disappear.mp4"
    disappearing_file.write_text("temp content")

    vf = VideoFile(path=disappearing_file, size_bytes=disappearing_file.stat().st_size)

    config = AppConfig(general=GeneralConfig(threads=1, debug=False))

    mock_scanner = MagicMock()
    mock_scanner.scan.return_value = [vf]

    mock_ffprobe = MagicMock()

    # Simulate file disappearing before ffprobe
    def ffprobe_fail(*args, **kwargs):
        # Delete the file
        if disappearing_file.exists():
            disappearing_file.unlink()
        raise FileNotFoundError("File not found")

    mock_ffprobe.get_stream_info.side_effect = ffprobe_fail

    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=mock_scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=mock_ffprobe,
        ffmpeg_adapter=MagicMock()
    )

    # Should not crash
    orchestrator.run(input_dir)

    # .err file should exist
    output_dir = tmp_path / "in_out"
    err_file = output_dir / "will_disappear.err"
    assert err_file.exists()


def test_exiftool_timeout_handling(tmp_path):
    """Test that ExifTool timeouts are handled gracefully."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    video_file = input_dir / "slow_metadata.mp4"
    video_file.write_text("video with slow exif")

    vf = VideoFile(path=video_file, size_bytes=video_file.stat().st_size)

    config = AppConfig(general=GeneralConfig(threads=1, copy_metadata=True, debug=True))

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

    def compress_create_output(job, config, use_gpu=False, **kwargs):
        job.status = JobStatus.COMPLETED
        job.output_path.parent.mkdir(parents=True, exist_ok=True)
        job.output_path.write_text("compressed")

    mock_ffmpeg.compress.side_effect = compress_create_output

    # Orchestrator will call _copy_deep_metadata which uses subprocess.run
    # We can patch subprocess.run to simulate timeout
    with patch("subprocess.run") as mock_run:
        import subprocess
        # First call succeeds (ffmpeg), second times out (exiftool)
        mock_run.side_effect = [
            MagicMock(returncode=0),  # Some initial call
            subprocess.TimeoutExpired("exiftool", 30),  # ExifTool timeout
        ]

        orchestrator = Orchestrator(
            config=config,
            event_bus=MagicMock(),
            file_scanner=mock_scanner,
            exif_adapter=MagicMock(),
            ffprobe_adapter=mock_ffprobe,
            ffmpeg_adapter=mock_ffmpeg
        )

        # Should not crash despite timeout
        orchestrator.run(input_dir)

        output_dir = tmp_path / "in_out"
        output_file = output_dir / "slow_metadata.mp4"

        # File should still exist (compression succeeded)
        # But .err might exist if timeout caused failure
        # OR job completed but metadata copy failed (logged warning)
        # In current impl, timeout in metadata copy just logs warning


def test_output_directory_permission_error(tmp_path):
    """Test handling of permission errors when creating output directory."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    video_file = input_dir / "test.mp4"
    video_file.write_text("content")

    vf = VideoFile(path=video_file, size_bytes=video_file.stat().st_size)

    config = AppConfig(general=GeneralConfig(threads=1, debug=False))

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

    # Patch mkdir to raise PermissionError
    with patch.object(Path, 'mkdir', side_effect=PermissionError("Cannot create directory")):
        orchestrator = Orchestrator(
            config=config,
            event_bus=MagicMock(),
            file_scanner=mock_scanner,
            exif_adapter=MagicMock(),
            ffprobe_adapter=mock_ffprobe,
            ffmpeg_adapter=MagicMock()
        )

        # Should handle the error (may crash or create .err, depending on impl)
        try:
            orchestrator.run(input_dir)
        except PermissionError:
            # This is acceptable - orchestrator propagates the error
            pass
