from pathlib import Path
from unittest.mock import MagicMock

from vbc.config.models import AppConfig, GeneralConfig, AutoRotateConfig
from vbc.domain.models import VideoFile
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.file_scanner import FileScanner
from vbc.pipeline.orchestrator import Orchestrator


def _make_orchestrator() -> Orchestrator:
    config = AppConfig(
        general=GeneralConfig(
            threads=1,
            gpu=False,
            extensions=[".mp4"],
            min_size_bytes=0,
            use_exif=False,
        ),
        autorotate=AutoRotateConfig(patterns={}),
    )
    scanner = FileScanner(config.general.extensions, config.general.min_size_bytes)
    return Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )


def _make_video(path: Path, size: int) -> VideoFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return VideoFile(path=path, size_bytes=size)


def test_move_completed_file_moves_when_destination_missing(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "input_out"
    input_dir.mkdir()
    output_dir.mkdir()

    source = input_dir / "video.mp4"
    video_file = _make_video(source, 128)

    orchestrator = _make_orchestrator()
    orchestrator._folder_mapping = {input_dir: output_dir}

    assert orchestrator._move_completed_file(video_file, output_dir) is True

    dest = output_dir / "video.mp4"
    assert dest.exists()
    assert not source.exists()
    assert dest.stat().st_size == 128


def test_move_completed_file_deletes_source_on_same_size_duplicate(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "input_out"
    input_dir.mkdir()
    output_dir.mkdir()

    source = input_dir / "video.mp4"
    video_file = _make_video(source, 256)

    dest = output_dir / "video.mp4"
    dest.write_bytes(b"y" * 256)

    orchestrator = _make_orchestrator()
    orchestrator._folder_mapping = {input_dir: output_dir}

    assert orchestrator._move_completed_file(video_file, output_dir) is True

    assert dest.exists()
    assert not source.exists()
    assert dest.stat().st_size == 256


def test_move_completed_file_renames_on_size_mismatch(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "input_out"
    input_dir.mkdir()
    output_dir.mkdir()

    source = input_dir / "video.mp4"
    video_file = _make_video(source, 300)

    dest = output_dir / "video.mp4"
    dest.write_bytes(b"z" * 200)

    orchestrator = _make_orchestrator()
    orchestrator._folder_mapping = {input_dir: output_dir}

    assert orchestrator._move_completed_file(video_file, output_dir) is True

    dup = output_dir / "video_vbc_dup.mp4"
    assert dest.exists()
    assert dup.exists()
    assert not source.exists()
    assert dest.stat().st_size == 200
    assert dup.stat().st_size == 300
