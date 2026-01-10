"""
Integration tests with real video files: metadata preservation.
"""
import pytest
import subprocess
import shutil
from vbc.config.models import AppConfig, GeneralConfig, AutoRotateConfig
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.file_scanner import FileScanner
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.infrastructure.ffprobe import FFprobeAdapter
from vbc.infrastructure.ffmpeg import FFmpegAdapter
from vbc.pipeline.orchestrator import Orchestrator


@pytest.mark.slow
@pytest.mark.integration
def test_real_file_metadata_preservation(real_test_videos):
    """Test that EXIF metadata is preserved after compression."""
    input_dir, files = real_test_videos

    gh7_only = input_dir / "gh7_only"
    gh7_only.mkdir()
    shutil.copy(input_dir / "gh7_test.mp4", gh7_only / "gh7_test.mp4")

    config = AppConfig(
        general=GeneralConfig(
            threads=1,
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

        orchestrator.run(gh7_only)

        output_dir = gh7_only.with_name(f"{gh7_only.name}_out")
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
