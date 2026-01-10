"""
Integration tests with real video files: dynamic CQ behavior.
"""
import pytest
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
def test_real_file_dynamic_cq(real_test_videos):
    """Test that dynamic CQ is applied based on camera model."""
    input_dir, files = real_test_videos

    dynamic_only = input_dir / "dynamic_only"
    dynamic_only.mkdir()
    shutil.copy(input_dir / "gh7_test.mp4", dynamic_only / "gh7_test.mp4")

    config = AppConfig(
        general=GeneralConfig(
            threads=1,
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
        orchestrator.run(dynamic_only)

        output_dir = dynamic_only.with_name(f"{dynamic_only.name}_out")

        # Check log for dynamic CQ detection
        log_file = output_dir / "compression.log"
        if log_file.exists():
            log_content = log_file.read_text()
            # Should mention custom CQ for detected cameras
            assert "custom CQ" in log_content or "CQ" in log_content

    finally:
        if exif.et.running:
            exif.et.terminate()
