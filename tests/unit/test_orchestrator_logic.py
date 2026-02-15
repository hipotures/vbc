"""Unit tests for Orchestrator logic (discovery, CQ, rotation, metadata)."""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from vbc.pipeline.orchestrator import Orchestrator
from vbc.config.models import AppConfig, GeneralConfig, AutoRotateConfig
from vbc.domain.models import VideoFile, VideoMetadata
from vbc.infrastructure.event_bus import EventBus


@pytest.fixture
def orchestrator_basic(tmp_path):
    """Create orchestrator with basic config."""
    config = AppConfig(
        general=GeneralConfig(threads=2, gpu=False),
        autorotate=AutoRotateConfig(patterns={})
    )

    bus = EventBus()
    scanner = MagicMock()
    exif = MagicMock()
    ffprobe = MagicMock()
    ffmpeg = MagicMock()

    orch = Orchestrator(
        config=config,
        event_bus=bus,
        file_scanner=scanner,
        exif_adapter=exif,
        ffprobe_adapter=ffprobe,
        ffmpeg_adapter=ffmpeg
    )

    return orch


def test_determine_cq_default(orchestrator_basic):
    """Test CQ determination with default config."""
    vf = VideoFile(
        path=Path("test.mp4"),
        size_bytes=1000,
        metadata=VideoMetadata(width=1920, height=1080, codec="h264", fps=30)
    )

    cq = orchestrator_basic._determine_cq(vf)
    assert cq == 32  # Default from CPU encoder args


def test_determine_cq_custom_from_metadata(orchestrator_basic):
    """Test CQ determination when metadata has custom_cq."""
    vf = VideoFile(
        path=Path("test.mp4"),
        size_bytes=1000,
        metadata=VideoMetadata(
            width=1920, height=1080, codec="h264", fps=30,
            camera_model="DC-GH7",
            custom_cq=30
        )
    )

    cq = orchestrator_basic._determine_cq(vf)
    assert cq == 30  # Custom from metadata


def test_determine_cq_dynamic_match():
    """Test CQ determination with dynamic_quality config."""
    config = AppConfig(
        general=GeneralConfig(
            threads=2, gpu=False,
            dynamic_quality={"DC-GH7": {"cq": 30}, "ILCE-7RM5": {"cq": 35}}
        ),
        autorotate=AutoRotateConfig(patterns={})
    )

    orch = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock()
    )

    vf = VideoFile(
        path=Path("test.mp4"),
        size_bytes=1000,
        metadata=VideoMetadata(
            width=1920, height=1080, codec="h264", fps=30,
            camera_model="DC-GH7"
        )
    )

    cq = orch._determine_cq(vf)
    assert cq == 30  # Matched from dynamic_quality


def test_determine_rate_control_dynamic_override():
    config = AppConfig(
        general=GeneralConfig(
            threads=2,
            gpu=False,
            quality_mode="rate",
            bps="200M",
            minrate="180M",
            maxrate="220M",
            dynamic_quality={
                "DC-GH7": {
                    "cq": 30,
                    "rate": {
                        "bps": "0.8",
                        "minrate": "0.7",
                        "maxrate": "0.9",
                    },
                }
            },
        ),
        autorotate=AutoRotateConfig(patterns={}),
    )

    orch = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )

    vf = VideoFile(
        path=Path("test.mp4"),
        size_bytes=1000,
        metadata=VideoMetadata(
            width=1920,
            height=1080,
            codec="h264",
            fps=30,
            camera_model="DC-GH7",
            bitrate_kbps=200000,
        ),
    )

    resolved = orch._determine_rate_control(vf)
    assert resolved.target_bps == 160000000
    assert resolved.minrate_bps == 140000000
    assert resolved.maxrate_bps == 180000000


def test_determine_cq_no_metadata(orchestrator_basic):
    """Test CQ determination when no metadata available."""
    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000, metadata=None)

    cq = orchestrator_basic._determine_cq(vf)
    assert cq == 32  # Fallback to default


def test_determine_rotation_manual():
    """Test rotation determination with manual override."""
    config = AppConfig(
        general=GeneralConfig(threads=2, gpu=False, manual_rotation=180),
        autorotate=AutoRotateConfig(patterns={})
    )

    orch = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock()
    )

    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
    rotation = orch._determine_rotation(vf)
    assert rotation == 180


def test_determine_rotation_pattern_match():
    """Test rotation determination with pattern matching."""
    config = AppConfig(
        general=GeneralConfig(threads=2, gpu=False),
        autorotate=AutoRotateConfig(patterns={
            r"QVR_\d{8}_\d{6}\.mp4": 180,
            r"ROT90_.*": 90
        })
    )

    orch = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock()
    )

    # Test QVR pattern
    vf1 = VideoFile(path=Path("QVR_20250101_120000.mp4"), size_bytes=1000)
    assert orch._determine_rotation(vf1) == 180

    # Test ROT90 pattern
    vf2 = VideoFile(path=Path("ROT90_test.mp4"), size_bytes=1000)
    assert orch._determine_rotation(vf2) == 90

    # Test no match
    vf3 = VideoFile(path=Path("normal.mp4"), size_bytes=1000)
    assert orch._determine_rotation(vf3) is None


def test_determine_rotation_no_patterns(orchestrator_basic):
    """Test rotation when no patterns configured."""
    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
    rotation = orchestrator_basic._determine_rotation(vf)
    assert rotation is None


def test_metadata_caching(orchestrator_basic):
    """Test that metadata is cached to avoid repeated calls."""
    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)

    # Mock ffprobe to return stream info
    orchestrator_basic.ffprobe_adapter.get_stream_info.return_value = {
        "width": 1920,
        "height": 1080,
        "codec": "h264",
        "fps": 30.0,
        "color_space": "bt709"
    }

    # First call - should call ffprobe
    meta1 = orchestrator_basic._get_metadata(vf)
    assert meta1 is not None
    assert meta1.width == 1920
    assert orchestrator_basic.ffprobe_adapter.get_stream_info.call_count == 1

    # Second call - should use cache
    meta2 = orchestrator_basic._get_metadata(vf)
    assert meta2 is not None
    assert meta2.width == 1920
    assert orchestrator_basic.ffprobe_adapter.get_stream_info.call_count == 1  # Not called again


def test_metadata_cache_different_files(orchestrator_basic):
    """Test that different files get different metadata."""
    vf1 = VideoFile(path=Path("test1.mp4"), size_bytes=1000)
    vf2 = VideoFile(path=Path("test2.mp4"), size_bytes=2000)

    orchestrator_basic.ffprobe_adapter.get_stream_info.side_effect = [
        {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "color_space": "bt709"},
        {"width": 3840, "height": 2160, "codec": "hevc", "fps": 60.0, "color_space": "bt709"}
    ]

    meta1 = orchestrator_basic._get_metadata(vf1)
    meta2 = orchestrator_basic._get_metadata(vf2)

    assert meta1.width == 1920
    assert meta2.width == 3840


def test_build_metadata_basic(orchestrator_basic):
    """Test metadata building from stream info."""
    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
    stream_info = {
        "width": 1920,
        "height": 1080,
        "codec": "h264",
        "fps": 30.0,
        "color_space": "bt709",
        "duration": 120.5
    }

    metadata = orchestrator_basic._build_metadata(vf, stream_info)

    assert metadata.width == 1920
    assert metadata.height == 1080
    assert metadata.codec == "h264"
    assert metadata.fps == 30.0
    assert metadata.color_space == "bt709"
    assert metadata.duration == 120.5
    assert metadata.megapixels == 2  # 1920*1080 / 1M


def test_build_metadata_with_exif(orchestrator_basic):
    """Test metadata building with ExifTool data."""
    orchestrator_basic.config.general.use_exif = True

    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
    stream_info = {
        "width": 1920,
        "height": 1080,
        "codec": "h264",
        "fps": 30.0,
        "color_space": "bt709"
    }

    # Mock ExifTool to return camera info
    orchestrator_basic.exif_adapter.extract_exif_info.return_value = {
        "camera_model": "DC-GH7",
        "camera_raw": "DC-GH7",
        "custom_cq": 30,
        "bitrate_kbps": 100000
    }

    metadata = orchestrator_basic._build_metadata(vf, stream_info)

    assert metadata.camera_model == "DC-GH7"
    assert metadata.custom_cq == 30
    assert metadata.bitrate_kbps == 100000


def test_build_metadata_keeps_ffprobe_bitrate_when_exif_missing(orchestrator_basic):
    """Test that EXIF bitrate=None does not clobber bitrate from ffprobe."""
    orchestrator_basic.config.general.use_exif = True

    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
    stream_info = {
        "width": 1920,
        "height": 1080,
        "codec": "h264",
        "fps": 30.0,
        "color_space": "bt709",
        "bitrate_kbps": 603979.776,
    }

    orchestrator_basic.exif_adapter.extract_exif_info.return_value = {
        "camera_model": "DC-GH7",
        "camera_raw": "DC-GH7",
        "custom_cq": 30,
        "bitrate_kbps": None,
    }

    metadata = orchestrator_basic._build_metadata(vf, stream_info)

    assert metadata.camera_model == "DC-GH7"
    assert metadata.custom_cq == 30
    assert metadata.bitrate_kbps == 603979.776


def test_build_metadata_exif_failure(orchestrator_basic):
    """Test metadata building when ExifTool fails."""
    orchestrator_basic.config.general.use_exif = True
    orchestrator_basic.config.general.debug = True

    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
    stream_info = {
        "width": 1920,
        "height": 1080,
        "codec": "h264",
        "fps": 30.0,
        "color_space": "bt709"
    }

    # Mock ExifTool to raise exception
    orchestrator_basic.exif_adapter.extract_exif_info.side_effect = Exception("ExifTool error")

    # Should not crash, just skip exif data
    metadata = orchestrator_basic._build_metadata(vf, stream_info)

    assert metadata.width == 1920
    assert metadata.camera_model is None


def test_check_and_fix_color_space_ok(orchestrator_basic, tmp_path):
    """Test color space check when color is OK."""
    input_file = tmp_path / "test.mp4"
    input_file.write_bytes(b"dummy")

    output_file = tmp_path / "output.mp4"

    stream_info = {
        "color_space": "bt709",
        "codec": "h264"
    }

    fixed_input, temp_file = orchestrator_basic._check_and_fix_color_space(
        input_file, output_file, stream_info
    )

    assert fixed_input == input_file  # No change
    assert temp_file is None  # No temp file created


def test_check_and_fix_color_space_reserved(orchestrator_basic, tmp_path):
    """Test color space fix when reserved."""
    input_file = tmp_path / "test.mp4"
    input_file.write_bytes(b"dummy video data")

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    output_file = output_dir / "test.mp4"

    stream_info = {
        "color_space": "reserved",
        "codec": "hevc"
    }

    # Mock subprocess to simulate successful fix and create temp file
    def mock_subprocess_run(cmd, *args, **kwargs):
        # Extract output file from command
        if str(output_dir / "test_colorfix.mp4") in [str(c) for c in cmd]:
            temp_file_path = output_dir / "test_colorfix.mp4"
            temp_file_path.write_bytes(b"fixed video data")

        result = MagicMock()
        result.returncode = 0
        return result

    with patch("subprocess.run", side_effect=mock_subprocess_run):
        fixed_input, temp_file = orchestrator_basic._check_and_fix_color_space(
            input_file, output_file, stream_info
        )

        # Should have created temp file with colorfix suffix
        assert temp_file is not None
        assert "_colorfix.mp4" in str(temp_file)
        assert temp_file.exists()


def test_check_and_fix_color_space_unsupported_codec(orchestrator_basic, tmp_path):
    """Test color space fix with unsupported codec."""
    input_file = tmp_path / "test.mp4"
    input_file.write_bytes(b"dummy")

    output_file = tmp_path / "output.mp4"

    stream_info = {
        "color_space": "reserved",
        "codec": "vp9"  # Not hevc or h264
    }

    fixed_input, temp_file = orchestrator_basic._check_and_fix_color_space(
        input_file, output_file, stream_info
    )

    # Should return original file without attempting fix
    assert fixed_input == input_file
    assert temp_file is None


def test_build_vbc_tag_args(orchestrator_basic, tmp_path):
    """Test VBC tag arguments building."""
    source = tmp_path / "source.mp4"

    args = orchestrator_basic._build_vbc_tag_args(
        source_path=source,
        quality_label="45",
        encoder="NVENC AV1 (GPU)",
        original_size=1000000,
        finished_at="2025-01-01T12:00:00"
    )

    assert "-XMP:VBCOriginalName=source.mp4" in args
    assert "-XMP:VBCOriginalSize=1000000" in args
    assert "-XMP:VBCQuality=45" in args
    assert "-XMP:VBCEncoder=NVENC AV1 (GPU)" in args
    assert "-XMP:VBCFinishedAt=2025-01-01T12:00:00" in args
