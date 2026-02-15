"""Unit tests for orchestrator.py error handling paths."""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import subprocess
from vbc.pipeline.orchestrator import Orchestrator
from vbc.config.models import AppConfig, GeneralConfig
from vbc.domain.models import VideoFile, VideoMetadata, CompressionJob, JobStatus


@pytest.fixture
def orchestrator(tmp_path):
    """Create orchestrator with mocked dependencies."""
    config = AppConfig(general=GeneralConfig(threads=1, debug=False, use_exif=True))

    mock_scanner = MagicMock()
    mock_exif = MagicMock()
    mock_ffprobe = MagicMock()
    mock_ffmpeg = MagicMock()
    mock_bus = MagicMock()

    return Orchestrator(
        config=config,
        event_bus=mock_bus,
        file_scanner=mock_scanner,
        exif_adapter=mock_exif,
        ffprobe_adapter=mock_ffprobe,
        ffmpeg_adapter=mock_ffmpeg
    )


class TestColorSpaceFixErrors:
    """Test color space fix error paths."""

    def test_color_space_fix_timeout(self, orchestrator, tmp_path):
        """Test timeout during color space fix remux."""
        input_file = tmp_path / "test.mp4"
        output_file = tmp_path / "output.mp4"
        input_file.write_text("dummy")

        stream_info = {
            'color_space': 'reserved',
            'codec': 'hevc',
            'width': 1920,
            'height': 1080
        }

        with patch('subprocess.run') as mock_run:
            # ffmpeg timeout
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=['ffmpeg'], timeout=300)

            # Call _check_and_fix_color_space
            result_file, temp_file = orchestrator._check_and_fix_color_space(input_file, output_file, stream_info)

            # Should return original file on timeout
            assert result_file == input_file
            assert temp_file is None

    def test_color_space_fix_failed(self, orchestrator, tmp_path):
        """Test failed color space fix (ffmpeg error)."""
        input_file = tmp_path / "test.mp4"
        output_file = tmp_path / "output.mp4"
        input_file.write_text("dummy")

        stream_info = {
            'color_space': 'reserved',
            'codec': 'hevc',
            'width': 1920,
            'height': 1080
        }

        with patch('subprocess.run') as mock_run:
            # ffmpeg fails
            mock_run.return_value = MagicMock(returncode=1, stderr="Error: Invalid stream")

            result_file, temp_file = orchestrator._check_and_fix_color_space(input_file, output_file, stream_info)

            # Should return original file on failure
            assert result_file == input_file
            assert temp_file is None

    def test_color_space_fix_tmp_missing(self, orchestrator, tmp_path):
        """Test color space fix when tmp file doesn't exist after ffmpeg."""
        input_file = tmp_path / "test.mp4"
        output_file = tmp_path / "output.mp4"
        input_file.write_text("dummy")

        stream_info = {
            'color_space': 'reserved',
            'codec': 'hevc',
            'width': 1920,
            'height': 1080
        }

        with patch('subprocess.run') as mock_run:
            # ffmpeg succeeds but tmp doesn't exist
            mock_run.return_value = MagicMock(returncode=0)

            result_file, temp_file = orchestrator._check_and_fix_color_space(input_file, output_file, stream_info)

            # Should return original file if tmp doesn't exist
            assert result_file == input_file
            assert temp_file is None


class TestDeepMetadataCopyErrors:
    """Test deep metadata copy error paths."""

    def test_copy_deep_metadata_timeout(self, orchestrator, tmp_path):
        """Test timeout during exiftool metadata copy."""
        source = tmp_path / "source.mp4"
        dest = tmp_path / "dest.mp4"
        err_path = tmp_path / "test.err"
        source.write_text("source")
        dest.write_text("dest")

        with patch('subprocess.run') as mock_run:
            # Simulate timeout
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=['exiftool'], timeout=30)

            # Should not raise, just log error
            orchestrator._copy_deep_metadata(
                source, dest, err_path,
                quality_label="45", encoder="av1_nvenc",
                original_size=1000, finished_at="2025-01-01 12:00:00"
            )

            # Verify exiftool was called
            assert mock_run.called

    def test_copy_deep_metadata_failed(self, orchestrator, tmp_path):
        """Test failed exiftool metadata copy."""
        source = tmp_path / "source.mp4"
        dest = tmp_path / "dest.mp4"
        err_path = tmp_path / "test.err"
        source.write_text("source")
        dest.write_text("dest")

        with patch('subprocess.run') as mock_run:
            # Simulate failure
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1,
                cmd=['exiftool'],
                stderr="Error: File not found"
            )

            # Should not raise, just log warning
            orchestrator._copy_deep_metadata(
                source, dest, err_path,
                quality_label="45", encoder="av1_nvenc",
                original_size=1000, finished_at="2025-01-01 12:00:00"
            )

            assert mock_run.called

    def test_copy_deep_metadata_generic_error(self, orchestrator, tmp_path):
        """Test generic error during metadata copy."""
        source = tmp_path / "source.mp4"
        dest = tmp_path / "dest.mp4"
        err_path = tmp_path / "test.err"
        source.write_text("source")
        dest.write_text("dest")

        with patch('subprocess.run') as mock_run:
            # Simulate generic exception
            mock_run.side_effect = OSError("Permission denied")

            # Should not raise
            orchestrator._copy_deep_metadata(
                source, dest, err_path,
                quality_label="45", encoder="av1_nvenc",
                original_size=1000, finished_at="2025-01-01 12:00:00"
            )

            assert mock_run.called


class TestProcessFileEdgeCases:
    """Test _process_file edge cases."""

    def test_process_file_output_collision(self, orchestrator, tmp_path):
        """Test skipping when output file already exists."""
        # Setup
        input_dir = tmp_path / "input"
        output_dir = tmp_path / "input_out"
        input_dir.mkdir()
        output_dir.mkdir()

        input_file = input_dir / "test.mp4"
        input_file.write_text("input")

        output_file = output_dir / "test.mp4"
        output_file.write_text("output")  # Already exists!

        video_file = VideoFile(path=input_file, size_bytes=1000)

        # Mock ffprobe to return valid metadata
        orchestrator.ffprobe_adapter.get_stream_info.return_value = {
            'width': 1920,
            'height': 1080,
            'codec': 'h264',
            'fps': 30.0,
            'color_space': None,
            'duration': 10.0
        }
        
        # Mock ExifTool to return empty dict (so vbc_encoded is False)
        orchestrator.exif_adapter.extract_exif_info.return_value = {}

        # Process file
        orchestrator._process_file(video_file, input_dir)

        # Should skip (JobFailed with collision message published)
        calls = orchestrator.event_bus.publish.call_args_list

        # Find JobFailed event
        failed_events = [call for call in calls if len(call[0]) > 0 and 'JobFailed' in str(type(call[0][0]))]
        assert len(failed_events) > 0



class TestMetadataExtractionErrors:
    """Test _get_metadata error handling."""

    def test_get_metadata_ffprobe_returns_none(self, orchestrator, tmp_path):
        """Test when ffprobe returns None (file unreadable)."""
        input_file = tmp_path / "test.mp4"
        input_file.write_text("dummy")

        video_file = VideoFile(path=input_file, size_bytes=1000)

        # Mock ffprobe to return None
        orchestrator.ffprobe_adapter.get_stream_info.return_value = None

        # Should return None
        metadata = orchestrator._get_metadata(video_file)
        assert metadata is None

    def test_get_metadata_exif_extraction_fails(self, orchestrator, tmp_path):
        """Test when ExifTool extraction fails."""
        input_file = tmp_path / "test.mp4"
        input_file.write_text("dummy")

        video_file = VideoFile(path=input_file, size_bytes=1000)

        # Mock ffprobe success
        orchestrator.ffprobe_adapter.get_stream_info.return_value = {
            'width': 1920,
            'height': 1080,
            'codec': 'h264',
            'fps': 30.0,
            'color_space': None,
            'duration': 10.0
        }

        # Mock exiftool to raise exception
        orchestrator.exif_adapter.extract_metadata.side_effect = Exception("ExifTool error")

        # Should still return metadata (just without EXIF camera info)
        metadata = orchestrator._get_metadata(video_file)
        assert metadata is not None
        assert metadata.width == 1920
        # EXIF failed, so no camera info extracted (depends on implementation)
