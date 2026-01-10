"""
Integration tests with real video files: compression behavior.
"""
import pytest
import subprocess
import shutil
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

    sony_only = input_dir / "sony_only"
    sony_only.mkdir()
    shutil.copy(input_dir / "sony_test.mp4", sony_only / "sony_test.mp4")

    config = AppConfig(
        general=GeneralConfig(
            threads=1,
            gpu=False,  # CPU mode for compatibility
            copy_metadata=True,
            use_exif=True,
            extensions=[".mp4"],
            min_size_bytes=0,
            filter_cameras=["ILCE-7RM5"],
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

        orchestrator.run(sony_only)

        # Verify output file exists
        output_dir = sony_only.with_name(f"{sony_only.name}_out")
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
