"""Unit tests for ffprobe.py failure scenarios and edge cases."""
import pytest
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess
from vbc.infrastructure.ffprobe import FFprobeAdapter


@pytest.fixture
def ffprobe():
    """Create FFprobeAdapter instance."""
    return FFprobeAdapter()


class TestFFprobeProcessFailures:
    """Test ffprobe subprocess failures."""

    def test_get_stream_info_file_not_found(self, ffprobe, tmp_path):
        """Test RuntimeError when ffprobe fails (file not found)."""
        missing_file = tmp_path / "missing.mp4"

        with patch('subprocess.run') as mock_run:
            # Simulate ffprobe failure
            mock_run.return_value = MagicMock(
                returncode=1,
                stderr="No such file or directory"
            )

            with pytest.raises(RuntimeError, match="ffprobe failed"):
                ffprobe.get_stream_info(missing_file)

    def test_get_stream_info_corrupted_json_output(self, ffprobe, tmp_path):
        """Test JSONDecodeError when ffprobe returns invalid JSON."""
        test_file = tmp_path / "test.mp4"
        test_file.write_text("dummy")

        with patch('subprocess.run') as mock_run:
            # Simulate corrupted JSON output
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="{ invalid json }"
            )

            with pytest.raises(json.JSONDecodeError):
                ffprobe.get_stream_info(test_file)

    def test_get_stream_info_no_video_stream(self, ffprobe, tmp_path):
        """Test ValueError when no video stream found."""
        test_file = tmp_path / "test.mp4"
        test_file.write_text("dummy")

        with patch('subprocess.run') as mock_run:
            # Valid JSON but no video stream (only audio)
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "streams": [
                        {"codec_type": "audio", "codec_name": "aac"}
                    ],
                    "format": {}
                })
            )

            with pytest.raises(ValueError, match="No video stream found"):
                ffprobe.get_stream_info(test_file)


class TestFPSParsingEdgeCases:
    """Test FPS parsing error paths."""

    def test_get_stream_info_invalid_fps_string(self, ffprobe, tmp_path):
        """Test FPS parsing with invalid string."""
        test_file = tmp_path / "test.mp4"
        test_file.write_text("dummy")

        with patch('subprocess.run') as mock_run:
            # FPS string is invalid (not a number or fraction)
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "streams": [{
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "avg_frame_rate": "invalid/fps"  # ValueError path
                    }],
                    "format": {"duration": "10.0"}
                })
            )

            result = ffprobe.get_stream_info(test_file)
            assert result['fps'] == 0.0  # Should fallback to 0

    def test_get_stream_info_fps_without_fraction(self, ffprobe, tmp_path):
        """Test FPS parsing when it's a plain number."""
        test_file = tmp_path / "test.mp4"
        test_file.write_text("dummy")

        with patch('subprocess.run') as mock_run:
            # FPS as plain number (no fraction)
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "streams": [{
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "avg_frame_rate": "30"  # Plain number
                    }],
                    "format": {"duration": "10.0"}
                })
            )

            result = ffprobe.get_stream_info(test_file)
            assert result['fps'] == 30

    def test_get_stream_info_fps_over_240_rejected(self, ffprobe, tmp_path):
        """Test FPS over 240 is rejected."""
        test_file = tmp_path / "test.mp4"
        test_file.write_text("dummy")

        with patch('subprocess.run') as mock_run:
            # FPS over 240 (likely timebase)
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "streams": [{
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "avg_frame_rate": "24000/1"  # 24000 fps - invalid timebase
                    }],
                    "format": {"duration": "10.0"}
                })
            )

            result = ffprobe.get_stream_info(test_file)
            assert result['fps'] == 0.0  # Rejected as invalid (over 240)


class TestDurationParsingFallbacks:
    """Test duration parsing fallback chain."""

    def test_duration_from_format_tags(self, ffprobe, tmp_path):
        """Test duration fallback to format tags."""
        test_file = tmp_path / "test.mp4"
        test_file.write_text("dummy")

        with patch('subprocess.run') as mock_run:
            # No format.duration, but has format.tags.DURATION
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "streams": [{
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "avg_frame_rate": "30/1"
                    }],
                    "format": {
                        "tags": {
                            "DURATION": "00:01:30.500"  # HH:MM:SS format
                        }
                    }
                })
            )

            result = ffprobe.get_stream_info(test_file)
            assert result['duration'] > 90  # Should be ~90.5 seconds

    def test_duration_from_bitrate_size(self, ffprobe, tmp_path):
        """Test duration calculation from bitrate and size (last resort)."""
        test_file = tmp_path / "test.mp4"
        test_file.write_text("dummy")

        with patch('subprocess.run') as mock_run:
            # No duration anywhere, calculate from bitrate/size
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "streams": [{
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "avg_frame_rate": "30/1"
                    }],
                    "format": {
                        "size": "1000000",  # 1MB
                        "bit_rate": "800000"  # 800 kbps
                    }
                })
            )

            result = ffprobe.get_stream_info(test_file)
            # duration = (size * 8) / bitrate = (1000000 * 8) / 800000 = 10 seconds
            assert result['duration'] == 10.0


class TestDurationTagParsing:
    """Test _parse_duration_tag edge cases."""

    def test_parse_duration_tag_empty_string(self, ffprobe):
        """Test parsing empty string duration."""
        result = ffprobe._parse_duration_tag("")
        assert result == 0.0

    def test_parse_duration_tag_mm_ss_format(self, ffprobe):
        """Test parsing MM:SS format."""
        result = ffprobe._parse_duration_tag("05:30")  # 5:30 = 330 seconds
        assert result == 330.0

    def test_parse_duration_tag_hh_mm_ss_format(self, ffprobe):
        """Test parsing HH:MM:SS format."""
        result = ffprobe._parse_duration_tag("01:05:30")  # 1:05:30 = 3930 seconds
        assert result == 3930.0

    def test_parse_duration_tag_invalid_time_format(self, ffprobe):
        """Test parsing invalid time format."""
        result = ffprobe._parse_duration_tag("invalid:time:format")
        assert result == 0.0

    def test_parse_duration_tag_invalid_numbers_in_time(self, ffprobe):
        """Test parsing time with invalid numbers."""
        result = ffprobe._parse_duration_tag("xx:yy")
        assert result == 0.0


class TestTimeBaseDurationParsing:
    """Test _parse_time_base_duration edge cases."""

    def test_parse_time_base_duration_none_inputs(self, ffprobe):
        """Test with None inputs."""
        result = ffprobe._parse_time_base_duration(None, "1/30")
        assert result == 0.0

        result = ffprobe._parse_time_base_duration(1000, None)
        assert result == 0.0

    def test_parse_time_base_duration_no_slash(self, ffprobe):
        """Test timebase without slash."""
        result = ffprobe._parse_time_base_duration(1000, "30")
        assert result == 0.0

    def test_parse_time_base_duration_zero_denominator(self, ffprobe):
        """Test timebase with zero denominator."""
        result = ffprobe._parse_time_base_duration(1000, "1/0")
        assert result == 0.0

    def test_parse_time_base_duration_zero_ticks(self, ffprobe):
        """Test with zero or negative ticks."""
        result = ffprobe._parse_time_base_duration(0, "1/30")
        assert result == 0.0

        result = ffprobe._parse_time_base_duration(-100, "1/30")
        assert result == 0.0

    def test_parse_time_base_duration_valid(self, ffprobe):
        """Test valid timebase duration calculation."""
        # ticks=300, timebase=1/30 -> 300 * (1/30) = 10 seconds
        result = ffprobe._parse_time_base_duration(300, "1/30")
        assert result == 10.0


class TestToFloatUtility:
    """Test _to_float utility method."""

    def test_to_float_valid_number(self, ffprobe):
        """Test converting valid numbers."""
        assert ffprobe._to_float("123.45") == 123.45
        assert ffprobe._to_float(100) == 100.0

    def test_to_float_invalid_input(self, ffprobe):
        """Test converting invalid input."""
        assert ffprobe._to_float("invalid") == 0.0
        assert ffprobe._to_float(None) == 0.0
        assert ffprobe._to_float("") == 0.0
