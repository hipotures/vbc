import json
from pathlib import Path
from unittest.mock import patch

import pytest

from vbc.infrastructure.ffprobe import FFprobeAdapter

def test_ffprobe_parse_streams():
    mock_output = {
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30/1",
                "avg_frame_rate": "30/1",
                "bit_rate": "5000000"
            }
        ],
        "format": {
            "duration": "10.0"
        }
    }
    
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = json.dumps(mock_output)
        mock_run.return_value.returncode = 0
        
        adapter = FFprobeAdapter()
        info = adapter.get_stream_info(Path("test.mp4"))
        
        assert info["width"] == 1920
        assert info["height"] == 1080
        assert info["codec"] == "h264"
        assert info["fps"] == 30.0
        assert info["duration"] == 10.0

def test_ffprobe_error():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "error"
        
        adapter = FFprobeAdapter()
        with pytest.raises(RuntimeError):
            adapter.get_stream_info(Path("test.mp4"))


def test_ffprobe_duration_fallback_from_tags():
    mock_output = {
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "25/1",
            }
        ],
        "format": {
            "duration": "0",
            "tags": {"DURATION": "00:00:05.00"}
        }
    }

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = json.dumps(mock_output)
        mock_run.return_value.returncode = 0

        adapter = FFprobeAdapter()
        info = adapter.get_stream_info(Path("test.flv"))

        assert info["duration"] == pytest.approx(5.0)


def test_ffprobe_duration_fallback_from_time_base():
    mock_output = {
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 640,
                "height": 360,
                "avg_frame_rate": "30/1",
                "duration_ts": "5000",
                "time_base": "1/1000",
            }
        ],
        "format": {
            "duration": "0"
        }
    }

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = json.dumps(mock_output)
        mock_run.return_value.returncode = 0

        adapter = FFprobeAdapter()
        info = adapter.get_stream_info(Path("test.flv"))

        assert info["duration"] == pytest.approx(5.0)


def test_ffprobe_no_audio_stream_sets_no_audio_codec():
    mock_output = {
        "streams": [
            {
                "index": 0,
                "codec_name": "av1",
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30/1",
            },
        ],
        "format": {
            "duration": "10.0"
        }
    }

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = json.dumps(mock_output)
        mock_run.return_value.returncode = 0

        adapter = FFprobeAdapter()
        info = adapter.get_stream_info(Path("test.mp4"))

    assert info["audio_codec"] == "no-audio"


def test_ffprobe_audio_codec_detected():
    mock_output = {
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30/1",
            },
            {
                "index": 1,
                "codec_name": "aac",
                "codec_type": "audio",
            },
        ],
        "format": {
            "duration": "10.0"
        }
    }

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = json.dumps(mock_output)
        mock_run.return_value.returncode = 0

        adapter = FFprobeAdapter()
        info = adapter.get_stream_info(Path("test.mp4"))

    assert info["audio_codec"] == "aac"


def test_ffprobe_part_info_counts_packets_and_normalizes_flv_timeline():
    mock_output = {
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 640,
                "height": 1280,
                "avg_frame_rate": "25/1",
                "nb_read_packets": "100",
            },
            {
                "index": 1,
                "codec_name": "aac",
                "codec_type": "audio",
                "nb_read_packets": "90",
            },
        ],
        "format": {
            "start_time": "60.0",
            "duration": "70.0",
            "bit_rate": "1000000",
        },
        "packets": [
            {
                "stream_index": 0,
                "pts_time": "148.859",
                "duration_time": "0.040",
            },
            {
                "stream_index": 1,
                "pts_time": "300.000",
                "duration_time": "1.000",
            },
            {
                "stream_index": 0,
                "pts_time": "158.819",
                "duration_time": "0.040",
            },
        ],
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = json.dumps(mock_output)
        mock_run.return_value.returncode = 0

        info = FFprobeAdapter().get_part_info(Path("part.mp4"))

    assert info["video_packets"] == 100
    assert info["audio_packets"] == 90
    assert info["duration"] == pytest.approx(10.0)
    mock_run.assert_called_once()
    assert "-count_packets" in mock_run.call_args.args[0]
    assert "-show_packets" in mock_run.call_args.args[0]


def test_ffprobe_part_info_unwraps_32_bit_millisecond_timestamps():
    wrap = (2**32) / 1000
    mock_output = {
        "streams": [
            {
                "index": 1,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 512,
                "height": 1024,
                "avg_frame_rate": "24/1",
                "nb_read_packets": "4",
            }
        ],
        "format": {"duration": "0"},
        "packets": [
            {"stream_index": 1, "pts_time": str(wrap + 33.6)},
            {"stream_index": 1, "pts_time": str(wrap + 153.9)},
            {"stream_index": 1, "pts_time": "153.8"},
            {
                "stream_index": 1,
                "pts_time": "1214.5",
                "duration_time": "0.04",
            },
        ],
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = json.dumps(mock_output)
        mock_run.return_value.returncode = 0

        info = FFprobeAdapter().get_part_info(Path("part.mp4"))

    assert info["duration"] == pytest.approx(1180.94)


def test_ffprobe_part_info_can_skip_packet_timeline_for_output_verification():
    mock_output = {
        "streams": [
            {
                "index": 0,
                "codec_name": "av1",
                "codec_type": "video",
                "width": 640,
                "height": 1280,
                "avg_frame_rate": "25/1",
                "nb_read_packets": "98",
            }
        ],
        "format": {"duration": "4.0"},
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = json.dumps(mock_output)
        mock_run.return_value.returncode = 0

        info = FFprobeAdapter().get_part_info(
            Path("output.mp4"),
            scan_packet_timeline=False,
        )

    assert info["video_packets"] == 98
    assert info["duration"] == pytest.approx(4.0)
    mock_run.assert_called_once()
    assert "-show_packets" not in mock_run.call_args.args[0]
