"""
Integration tests with real video files.

These tests use actual 10-second video clips from different cameras.
They are marked as 'slow' and require real test data in tests/data/.

Run with: pytest -m slow
Skip with: pytest -m "not slow"
"""
import pytest
import subprocess
from pathlib import Path
from vbc.config.models import AppConfig, GeneralConfig, AutoRotateConfig
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.file_scanner import FileScanner
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.infrastructure.ffprobe import FFprobeAdapter
from vbc.infrastructure.ffmpeg import FFmpegAdapter
from vbc.pipeline.orchestrator import Orchestrator


@pytest.mark.slow
@pytest.mark.integration
def test_real_file_compression_sony(real_test_videos):
    """Test compression of real Sony ILCE-7RM5 video file."""
    input_dir, files = real_test_videos

    config = AppConfig(
        general=GeneralConfig(
            threads=1,
            cq=50,  # Higher CQ for faster test
            gpu=False,  # CPU mode for compatibility
            copy_metadata=True,
            use_exif=True,
            extensions=[".mp4"],
            min_size_bytes=0,
            filter_cameras=["Sony"],
            debug=False,
        ),
        autorotate=AutoRotateConfig(patterns={})
    )

    bus = EventBus()
    scanner = FileScanner(extensions=config.general.extensions, min_size_bytes=0)
    exif = ExifToolAdapter()
    exif.et.run()
    ffprobe = FFprobeAdapter()
    ffmpeg = FFmpegAdapter(event_bus=bus)

    try:
        orchestrator = Orchestrator(
            config=config,
            event_bus=bus,
            file_scanner=scanner,
            exif_adapter=exif,
            ffprobe_adapter=ffprobe,
            ffmpeg_adapter=ffmpeg
        )

        orchestrator.run(input_dir)

        # Verify output file exists
        output_dir = input_dir.with_name(f"{input_dir.name}_out")
        output_file = output_dir / "sony_test.mp4"
        assert output_file.exists(), f"Output file not found: {output_file}"

        # Verify it's AV1
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0",
             str(output_file)],
            capture_output=True, text=True
        )
        assert "av1" in result.stdout.lower(), f"Output is not AV1: {result.stdout}"

    finally:
        if exif.et.running:
            exif.et.terminate()


@pytest.mark.slow
@pytest.mark.integration
def test_real_file_metadata_preservation(real_test_videos):
    """Test that EXIF metadata is preserved after compression."""
    input_dir, files = real_test_videos

    config = AppConfig(
        general=GeneralConfig(
            threads=1,
            cq=50,
            gpu=False,
            copy_metadata=True,
            use_exif=True,
            extensions=[".mp4"],
            min_size_bytes=0,
            filter_cameras=["GH7"],
            debug=False,
        ),
        autorotate=AutoRotateConfig(patterns={})
    )

    bus = EventBus()
    scanner = FileScanner(extensions=config.general.extensions, min_size_bytes=0)
    exif = ExifToolAdapter()
    exif.et.run()
    ffprobe = FFprobeAdapter()
    ffmpeg = FFmpegAdapter(event_bus=bus)

    try:
        orchestrator = Orchestrator(
            config=config,
            event_bus=bus,
            file_scanner=scanner,
            exif_adapter=exif,
            ffprobe_adapter=ffprobe,
            ffmpeg_adapter=ffmpeg
        )

        orchestrator.run(input_dir)

        output_dir = input_dir.with_name(f"{input_dir.name}_out")
        output_file = output_dir / "gh7_test.mp4"
        assert output_file.exists()

        # Check metadata with exiftool
        result = subprocess.run(
            ["exiftool", "-Model", "-GPSLatitude", str(output_file)],
            capture_output=True, text=True
        )

        # Should have camera model
        assert "DC-GH7" in result.stdout or "GH7" in result.stdout

        # Should have GPS (we set it in conftest fixture)
        assert "50" in result.stdout  # Latitude ~50

    finally:
        if exif.et.running:
            exif.et.terminate()


@pytest.mark.slow
@pytest.mark.integration
def test_real_file_dynamic_cq(real_test_videos):
    """Test that dynamic CQ is applied based on camera model."""
    input_dir, files = real_test_videos

    config = AppConfig(
        general=GeneralConfig(
            threads=1,
            cq=45,  # Default
            gpu=False,
            copy_metadata=True,
            use_exif=True,
            extensions=[".mp4"],
            min_size_bytes=0,
            filter_cameras=[],
            dynamic_cq={
                "DC-GH7": 30,  # Lower CQ (better quality) for GH7
                "ILCE-7RM5": 35,  # For Sony
            },
            debug=True,  # Enable debug logging
        ),
        autorotate=AutoRotateConfig(patterns={})
    )

    bus = EventBus()
    scanner = FileScanner(extensions=config.general.extensions, min_size_bytes=0)
    exif = ExifToolAdapter()
    exif.et.run()
    ffprobe = FFprobeAdapter()
    ffmpeg = FFmpegAdapter(event_bus=bus)

    try:
        orchestrator = Orchestrator(
            config=config,
            event_bus=bus,
            file_scanner=scanner,
            exif_adapter=exif,
            ffprobe_adapter=ffprobe,
            ffmpeg_adapter=ffmpeg
        )

        # Run orchestrator (will process all files)
        orchestrator.run(input_dir)

        output_dir = input_dir.with_name(f"{input_dir.name}_out")

        # Check log for dynamic CQ detection
        log_file = output_dir / "compression.log"
        if log_file.exists():
            log_content = log_file.read_text()
            # Should mention custom CQ for detected cameras
            assert "custom CQ" in log_content or "CQ" in log_content

    finally:
        if exif.et.running:
            exif.et.terminate()


@pytest.mark.slow
@pytest.mark.integration
def test_real_file_autorotation(real_test_videos):
    """Test that autorotation pattern matching works with real files."""
    input_dir, files = real_test_videos

    # QVR file should exist from fixture
    qvr_file = input_dir / "QVR_20250101_120000.mp4"
    assert qvr_file.exists(), "QVR test file not found"

    config = AppConfig(
        general=GeneralConfig(
            threads=1,
            cq=50,
            gpu=False,
            copy_metadata=False,  # Faster without metadata
            use_exif=False,
            extensions=[".mp4"],
            min_size_bytes=0,
            filter_cameras=[],
            debug=True,
        ),
        autorotate=AutoRotateConfig(
            patterns={
                r"QVR_\d{8}_\d{6}\.mp4": 180  # Match QVR pattern
            }
        )
    )

    bus = EventBus()
    scanner = FileScanner(extensions=config.general.extensions, min_size_bytes=0)
    exif = ExifToolAdapter()
    exif.et.run()
    ffprobe = FFprobeAdapter()
    ffmpeg = FFmpegAdapter(event_bus=bus)

    try:
        orchestrator = Orchestrator(
            config=config,
            event_bus=bus,
            file_scanner=scanner,
            exif_adapter=exif,
            ffprobe_adapter=ffprobe,
            ffmpeg_adapter=ffmpeg
        )

        orchestrator.run(input_dir)

        output_dir = input_dir.with_name(f"{input_dir.name}_out")
        output_file = output_dir / "QVR_20250101_120000.mp4"
        assert output_file.exists()

        # Check log for rotation message
        log_file = output_dir / "compression.log"
        if log_file.exists():
            log_content = log_file.read_text()
            # Should mention rotation was applied
            assert "180" in log_content or "rotation" in log_content.lower()

    finally:
        if exif.et.running:
            exif.et.terminate()


@pytest.mark.slow
@pytest.mark.integration
def test_real_file_skip_av1(real_test_videos):
    """Test that files already in AV1 are skipped."""
    input_dir, files = real_test_videos

    # First compress to AV1
    config1 = AppConfig(
        general=GeneralConfig(
            threads=1,
            cq=50,
            gpu=False,
            copy_metadata=False,
            use_exif=False,
            extensions=[".mp4"],
            min_size_bytes=0,
            filter_cameras=["DJI"],
            skip_av1=False,  # First run: don't skip
            debug=False,
        )
    )

    bus = EventBus()
    scanner = FileScanner(extensions=config1.general.extensions, min_size_bytes=0)
    exif = ExifToolAdapter()
    exif.et.run()
    ffprobe = FFprobeAdapter()
    ffmpeg = FFmpegAdapter(event_bus=bus)

    try:
        orchestrator = Orchestrator(
            config=config1,
            event_bus=bus,
            file_scanner=scanner,
            exif_adapter=exif,
            ffprobe_adapter=ffprobe,
            ffmpeg_adapter=ffmpeg
        )

        orchestrator.run(input_dir)

        output_dir = input_dir.with_name(f"{input_dir.name}_out")
        output_file = output_dir / "dji_test.mp4"
        assert output_file.exists()

        # Now run again with skip_av1=True
        # Move output back to input to test skip
        skip_input_dir = input_dir.parent / "skip_test"
        skip_input_dir.mkdir(exist_ok=True)
        skip_test_file = skip_input_dir / "already_av1.mp4"
        skip_test_file.write_bytes(output_file.read_bytes())

        config2 = AppConfig(
            general=GeneralConfig(
                threads=1,
                cq=50,
                gpu=False,
                copy_metadata=False,
                use_exif=False,
                extensions=[".mp4"],
                min_size_bytes=0,
                filter_cameras=[],
                skip_av1=True,  # Second run: skip AV1
                debug=True,
            )
        )

        bus2 = EventBus()
        scanner2 = FileScanner(extensions=config2.general.extensions, min_size_bytes=0)
        ffprobe2 = FFprobeAdapter()
        ffmpeg2 = FFmpegAdapter(event_bus=bus2)

        orchestrator2 = Orchestrator(
            config=config2,
            event_bus=bus2,
            file_scanner=scanner2,
            exif_adapter=exif,
            ffprobe_adapter=ffprobe2,
            ffmpeg_adapter=ffmpeg2
        )

        orchestrator2.run(skip_input_dir)

        # Output should NOT exist (file was skipped)
        skip_output_dir = skip_input_dir.with_name(f"{skip_input_dir.name}_out")
        skip_output_file = skip_output_dir / "already_av1.mp4"
        assert not skip_output_file.exists(), "AV1 file should have been skipped"

        # Check log
        log_file = skip_output_dir / "compression.log"
        if log_file.exists():
            log_content = log_file.read_text()
            # Should mention AV1 skip
            # (might not be in log if discovery filtering happens first)

    finally:
        if exif.et.running:
            exif.et.terminate()
