"""
Integration tests with real video files: autorotation behavior.
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
def test_real_file_autorotation(real_test_videos):
    """Test that autorotation pattern matching works with real files."""
    input_dir, files = real_test_videos

    # QVR file should exist from fixture
    qvr_file = input_dir / "QVR_20250101_120000.mp4"
    assert qvr_file.exists(), "QVR test file not found"

    autorotation_dir = input_dir / "qvr_only"
    autorotation_dir.mkdir()
    shutil.copy(qvr_file, autorotation_dir / qvr_file.name)

    config = AppConfig(
        general=GeneralConfig(
            threads=1,
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

        orchestrator.run(autorotation_dir)

        output_dir = autorotation_dir.with_name(f"{autorotation_dir.name}_out")
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
