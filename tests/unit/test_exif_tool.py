import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.domain.models import VideoFile

def test_extract_metadata():
    mock_exif_data = [{
        "SourceFile": "test.mp4",
        "QuickTime:ImageWidth": 1920,
        "QuickTime:ImageHeight": 1080,
        "QuickTime:VideoFrameRate": 60,
        "QuickTime:CompressorID": "hvc1",
        "QuickTime:Model": "DJI Osmo Pocket 3",
        "QuickTime:AvgBitrate": 100000000
    }]

    with patch("exiftool.ExifTool") as MockExifTool:
        instance = MockExifTool.return_value
        instance.running = True
        instance.execute_json.return_value = mock_exif_data

        adapter = ExifToolAdapter()
        vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
        metadata = adapter.extract_metadata(vf)

        assert metadata.width == 1920
        assert metadata.height == 1080
        assert metadata.fps == 60
        assert metadata.codec == "hevc"  # hvc1 maps to hevc
        assert metadata.camera_model == "DJI Osmo Pocket 3"

def test_copy_metadata():
    with patch("exiftool.ExifTool") as MockExifTool:
        instance = MockExifTool.return_value
        adapter = ExifToolAdapter()
        
        adapter.copy_metadata(Path("src.mp4"), Path("dest.mp4"))
        
        # Verify exiftool was called with correct arguments for copying
        instance.execute.assert_called()
        args = instance.execute.call_args[0]
        assert "-tagsFromFile" in args
        assert "src.mp4" in args
        assert "dest.mp4" in args


def test_get_tag_missing_returns_none():
    with patch("exiftool.ExifTool"):
        adapter = ExifToolAdapter()
        assert adapter._get_tag({}, ["Missing", "AlsoMissing"]) is None


def test_extract_camera_raw_no_tags_returns_none():
    with patch("exiftool.ExifTool"):
        adapter = ExifToolAdapter()
        assert adapter._extract_camera_raw({"QuickTime:CompressorID": "hvc1"}) is None


def test_extract_metadata_runs_exiftool_and_raises_on_empty():
    with patch("exiftool.ExifTool") as MockExifTool:
        instance = MockExifTool.return_value
        instance.running = False
        instance.execute_json.return_value = []

        adapter = ExifToolAdapter()
        vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)

        with pytest.raises(ValueError):
            adapter.extract_metadata(vf)

        instance.run.assert_called_once()


def test_extract_metadata_unknown_codec_and_missing_fields():
    mock_exif_data = [{
        "QuickTime:CompressorID": "XYZ123"
    }]

    with patch("exiftool.ExifTool") as MockExifTool:
        instance = MockExifTool.return_value
        instance.running = True
        instance.execute_json.return_value = mock_exif_data

        adapter = ExifToolAdapter()
        vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
        metadata = adapter.extract_metadata(vf)

        assert metadata.width == 0
        assert metadata.height == 0
        assert metadata.fps == 0.0
        assert metadata.codec == "XYZ123"
        assert metadata.camera_model is None
        assert metadata.bitrate_kbps is None


def test_extract_exif_info_dynamic_cq_match_and_bitrate():
    mock_exif_data = [{
        "EXIF:Model": "Sony A7",
        "QuickTime:AvgBitrate": 2000
    }]

    with patch("exiftool.ExifTool") as MockExifTool:
        instance = MockExifTool.return_value
        instance.running = False
        instance.execute_json.return_value = mock_exif_data

        adapter = ExifToolAdapter()
        vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
        info = adapter.extract_exif_info(vf, {"Sony": 33})

        assert info["camera_model"] == "Sony A7"
        assert info["camera_raw"] == "Sony A7"
        assert info["custom_cq"] == 33
        assert info["bitrate_kbps"] == 2.0
        instance.run.assert_called_once()


def test_extract_exif_info_fallback_to_camera_raw():
    mock_exif_data = [{
        "QuickTime:Model": "Canon R5"
    }]

    with patch("exiftool.ExifTool") as MockExifTool:
        instance = MockExifTool.return_value
        instance.running = True
        instance.execute_json.return_value = mock_exif_data

        adapter = ExifToolAdapter()
        vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
        info = adapter.extract_exif_info(vf, {"Sony": 33})

        assert info["camera_model"] == "Canon R5"
        assert info["custom_cq"] is None
        assert info["bitrate_kbps"] is None


def test_extract_exif_info_raises_on_empty():
    with patch("exiftool.ExifTool") as MockExifTool:
        instance = MockExifTool.return_value
        instance.running = True
        instance.execute_json.return_value = []

        adapter = ExifToolAdapter()
        vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)

        with pytest.raises(ValueError):
            adapter.extract_exif_info(vf, {})


def test_copy_metadata_starts_exiftool_if_not_running():
    with patch("exiftool.ExifTool") as MockExifTool:
        instance = MockExifTool.return_value
        instance.running = False
        adapter = ExifToolAdapter()

        adapter.copy_metadata(Path("src.mp4"), Path("dest.mp4"))

        instance.run.assert_called_once()
        instance.execute.assert_called()
