import os
import subprocess
from unittest.mock import MagicMock, patch

from vbc.config.models import AppConfig, GeneralConfig, AutoRotateConfig
from vbc.domain.events import ActionMessage, JobCompleted, JobFailed, ProcessingFinished
from vbc.domain.models import JobStatus, VideoFile, VideoMetadata
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.file_scanner import FileScanner
from vbc.pipeline.orchestrator import Orchestrator


def _make_config(**kwargs):
    kwargs.setdefault("min_size_bytes", 0)
    general = GeneralConfig(threads=1, cq=45, gpu=False, **kwargs)
    return AppConfig(general=general, autorotate=AutoRotateConfig(patterns={}))


def test_perform_discovery_counts_and_skips(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    output_dir = tmp_path / "input_out"
    output_dir.mkdir()

    small = input_dir / "small.mp4"
    small.write_bytes(b"x" * 10)
    good = input_dir / "good.mp4"
    good.write_bytes(b"x" * 200)
    bad = input_dir / "bad.mp4"
    bad.write_bytes(b"x" * 200)
    hw = input_dir / "hw.mp4"
    hw.write_bytes(b"x" * 200)
    done = input_dir / "done.mp4"
    done.write_bytes(b"x" * 200)

    (output_dir / "bad.err").write_text("Something failed")
    (output_dir / "hw.err").write_text("Hardware is lacking required capabilities")

    done_out = output_dir / "done.mp4"
    done_out.write_bytes(b"x" * 50)
    os.utime(done_out, (done.stat().st_atime + 10, done.stat().st_mtime + 10))

    config = _make_config(min_size_bytes=100, extensions=[".mp4"], use_exif=False)
    scanner = FileScanner(config.general.extensions, config.general.min_size_bytes)
    orchestrator = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )

    files, stats = orchestrator._perform_discovery(input_dir)

    assert stats["files_found"] == 5
    assert stats["ignored_small"] == 1
    assert stats["already_compressed"] == 1
    assert stats["ignored_err"] == 1
    assert [vf.path.name for vf in files] == ["good.mp4"]


def test_perform_discovery_hw_cap_err_cleared_with_cpu_fallback(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    output_dir = tmp_path / "input_out"
    output_dir.mkdir()

    source = input_dir / "video.mp4"
    source.write_bytes(b"x" * 200)

    err_path = output_dir / "video.err"
    err_path.write_text("Hardware is lacking required capabilities")

    config = _make_config(cpu_fallback=True, extensions=[".mp4"], use_exif=False)
    scanner = FileScanner(config.general.extensions, config.general.min_size_bytes)
    orchestrator = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )

    files, stats = orchestrator._perform_discovery(input_dir)

    assert not err_path.exists()
    assert stats["ignored_err"] == 0
    assert [vf.path.name for vf in files] == ["video.mp4"]


def test_perform_discovery_sort_dir(tmp_path):
    input_a = tmp_path / "input_a"
    input_b = tmp_path / "input_b"
    input_a.mkdir()
    input_b.mkdir()

    (input_b / "m.mp4").write_bytes(b"x" * 50)
    (input_b / "a.mp4").write_bytes(b"x" * 50)
    sub = input_a / "sub"
    sub.mkdir()
    (sub / "a.mp4").write_bytes(b"x" * 50)
    (input_a / "z.mp4").write_bytes(b"x" * 50)

    config = _make_config(queue_sort="dir", extensions=[".mp4"], use_exif=False)
    scanner = FileScanner(config.general.extensions, config.general.min_size_bytes)
    orchestrator = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )

    files, _stats = orchestrator._perform_discovery([input_b, input_a])

    assert [vf.path for vf in files] == [
        input_b / "a.mp4",
        input_b / "m.mp4",
        sub / "a.mp4",
        input_a / "z.mp4",
    ]


def test_perform_discovery_sort_ext(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    (input_dir / "b.mp4").write_bytes(b"x" * 10)
    (input_dir / "a.mov").write_bytes(b"x" * 10)
    (input_dir / "c.mov").write_bytes(b"x" * 10)

    config = _make_config(queue_sort="ext", extensions=[".mov", ".mp4"], use_exif=False)
    scanner = FileScanner(config.general.extensions, config.general.min_size_bytes)
    orchestrator = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )

    files, _stats = orchestrator._perform_discovery(input_dir)

    assert [vf.path.name for vf in files] == ["a.mov", "c.mov", "b.mp4"]


def test_perform_discovery_sort_size_desc(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    small = input_dir / "small.mp4"
    medium = input_dir / "medium.mp4"
    large = input_dir / "large.mp4"
    small.write_bytes(b"x" * 10)
    medium.write_bytes(b"x" * 20)
    large.write_bytes(b"x" * 30)

    config = _make_config(queue_sort="size-desc", extensions=[".mp4"], use_exif=False)
    scanner = FileScanner(config.general.extensions, config.general.min_size_bytes)
    orchestrator = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )

    files, _stats = orchestrator._perform_discovery(input_dir)

    assert [vf.path.name for vf in files] == ["large.mp4", "medium.mp4", "small.mp4"]


def test_perform_discovery_sort_rand_seed(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    (input_dir / "a.mp4").write_bytes(b"x" * 10)
    (input_dir / "b.mp4").write_bytes(b"x" * 10)
    (input_dir / "c.mp4").write_bytes(b"x" * 10)

    config = _make_config(queue_sort="rand", queue_seed=123, extensions=[".mp4"], use_exif=False)
    scanner = FileScanner(config.general.extensions, config.general.min_size_bytes)
    orchestrator = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )

    first, _stats = orchestrator._perform_discovery(input_dir)
    second, _stats = orchestrator._perform_discovery(input_dir)

    assert [vf.path.name for vf in first] == [vf.path.name for vf in second]


def test_process_file_cpu_fallback_on_hw_cap(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    source = input_dir / "video.mp4"
    source.write_bytes(b"x" * 200)

    config = _make_config(
        gpu=True,
        cpu_fallback=True,
        ffmpeg_cpu_threads=2,
        use_exif=False,
        copy_metadata=False,
    )
    bus = EventBus()
    events = []
    bus.subscribe(JobCompleted, lambda e: events.append(e))

    ffprobe = MagicMock()
    ffprobe.get_stream_info.return_value = {
        "width": 1920,
        "height": 1080,
        "codec": "h264",
        "fps": 30.0,
        "duration": 1.0,
        "color_space": "bt709",
    }

    class DummyFFmpeg:
        def __init__(self):
            self.calls = []

        def compress(self, job, config, rotate=None, shutdown_event=None, input_path=None):
            self.calls.append(config.gpu)
            if config.gpu:
                job.status = JobStatus.HW_CAP_LIMIT
                job.error_message = "Hardware is lacking required capabilities"
            else:
                job.status = JobStatus.COMPLETED

    ffmpeg = DummyFFmpeg()
    orchestrator = Orchestrator(
        config=config,
        event_bus=bus,
        file_scanner=FileScanner([".mp4"], 0),
        exif_adapter=MagicMock(),
        ffprobe_adapter=ffprobe,
        ffmpeg_adapter=ffmpeg,
    )

    video_file = VideoFile(path=source, size_bytes=source.stat().st_size)
    orchestrator._process_file(video_file, input_dir)

    assert ffmpeg.calls == [True, False]
    assert events
    assert events[0].job.status == JobStatus.COMPLETED
    assert not (input_dir.with_name(f"{input_dir.name}_out") / "video.err").exists()


def test_process_file_skips_existing_error(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    output_dir = tmp_path / "input_out"
    output_dir.mkdir()

    source = input_dir / "video.mp4"
    source.write_bytes(b"x" * 200)
    (output_dir / "video.err").write_text("Previous error marker")

    config = _make_config(clean_errors=False, use_exif=False, copy_metadata=False)
    bus = EventBus()
    events = []
    bus.subscribe(JobFailed, lambda e: events.append(e))

    ffprobe = MagicMock()
    orchestrator = Orchestrator(
        config=config,
        event_bus=bus,
        file_scanner=FileScanner([".mp4"], 0),
        exif_adapter=MagicMock(),
        ffprobe_adapter=ffprobe,
        ffmpeg_adapter=MagicMock(),
    )

    video_file = VideoFile(path=source, size_bytes=source.stat().st_size)
    orchestrator._process_file(video_file, input_dir)

    assert events
    assert events[0].job.status == JobStatus.SKIPPED
    assert events[0].error_message == "Existing error marker found"
    assert ffprobe.get_stream_info.call_count == 0


def test_process_file_ffprobe_failure_writes_err(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    source = input_dir / "corrupt.mp4"
    source.write_bytes(b"x" * 200)

    config = _make_config(use_exif=False, copy_metadata=False)
    bus = EventBus()
    events = []
    bus.subscribe(JobFailed, lambda e: events.append(e))

    ffprobe = MagicMock()
    ffprobe.get_stream_info.side_effect = Exception("ffprobe failed")

    orchestrator = Orchestrator(
        config=config,
        event_bus=bus,
        file_scanner=FileScanner([".mp4"], 0),
        exif_adapter=MagicMock(),
        ffprobe_adapter=ffprobe,
        ffmpeg_adapter=MagicMock(),
    )

    video_file = VideoFile(path=source, size_bytes=source.stat().st_size)
    orchestrator._process_file(video_file, input_dir)

    output_dir = input_dir.with_name("input_out")
    err_path = output_dir / "corrupt.err"
    assert err_path.exists()
    assert "ffprobe failed to read" in err_path.read_text()
    assert events
    assert events[0].job.status == JobStatus.FAILED


def test_process_file_success_ratio_keeps_original(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    source = input_dir / "video.mp4"
    source.write_bytes(b"a" * 1000)

    config = _make_config(use_exif=False, copy_metadata=False, min_compression_ratio=0.1)
    bus = EventBus()
    events = []
    bus.subscribe(JobCompleted, lambda e: events.append(e))

    ffprobe = MagicMock()
    ffprobe.get_stream_info.return_value = {
        "width": 1920,
        "height": 1080,
        "codec": "h264",
        "fps": 30.0,
    }

    ffmpeg = MagicMock()

    def fake_compress(job, job_config, rotate=None, shutdown_event=None, input_path=None):
        job.status = JobStatus.COMPLETED
        job.output_path.write_bytes(b"b" * 950)

    ffmpeg.compress.side_effect = fake_compress

    orchestrator = Orchestrator(
        config=config,
        event_bus=bus,
        file_scanner=FileScanner([".mp4"], 0),
        exif_adapter=MagicMock(),
        ffprobe_adapter=ffprobe,
        ffmpeg_adapter=ffmpeg,
    )
    orchestrator._check_and_fix_color_space = MagicMock(return_value=(source, None))
    orchestrator._write_vbc_tags = MagicMock()

    video_file = VideoFile(path=source, size_bytes=source.stat().st_size)
    orchestrator._process_file(video_file, input_dir)

    assert events
    job = events[0].job
    assert job.status == JobStatus.COMPLETED
    assert "Ratio" in (job.error_message or "")

    output_path = input_dir.with_name("input_out") / "video.mp4"
    assert output_path.read_bytes() == source.read_bytes()
    orchestrator._write_vbc_tags.assert_called_once()


def test_run_refresh_adds_new_files(monkeypatch, tmp_path):
    config = _make_config(use_exif=False, copy_metadata=False, prefetch_factor=1)
    bus = EventBus()
    orchestrator = Orchestrator(
        config=config,
        event_bus=bus,
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )

    vf1 = VideoFile(path=tmp_path / "a.mp4", size_bytes=10)
    vf2 = VideoFile(path=tmp_path / "b.mp4", size_bytes=10)
    stats1 = {
        "files_found": 1,
        "files_to_process": 1,
        "already_compressed": 0,
        "ignored_small": 0,
        "ignored_err": 0,
    }
    stats2 = {
        "files_found": 2,
        "files_to_process": 2,
        "already_compressed": 0,
        "ignored_small": 0,
        "ignored_err": 0,
    }

    orchestrator._perform_discovery = MagicMock(side_effect=[([vf1], stats1), ([vf2], stats2)])
    orchestrator._get_metadata = MagicMock(
        return_value=VideoMetadata(width=1, height=1, codec="h264", fps=1.0)
    )
    orchestrator._process_file = MagicMock()
    orchestrator._refresh_requested = True

    messages = []
    bus.subscribe(ActionMessage, lambda e: messages.append(e.message))
    bus.subscribe(ProcessingFinished, lambda e: messages.append("finished"))

    monkeypatch.setattr("vbc.pipeline.orchestrator.time.sleep", lambda *_: None)

    orchestrator.run(tmp_path)

    assert "Refreshed: +1 new files" in messages
    assert "finished" in messages
    assert orchestrator._process_file.call_count == 2


def test_copy_deep_metadata_timeout_writes_err(tmp_path):
    config = _make_config(use_exif=False, copy_metadata=True, debug=True)
    orchestrator = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=FileScanner([".mp4"], 0),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )

    source = tmp_path / "src.mp4"
    source.write_bytes(b"x")
    output = tmp_path / "out.mp4"
    output.write_bytes(b"x")
    err_path = tmp_path / "out.err"

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="exiftool", timeout=30)

    with patch("subprocess.run", side_effect=fake_run):
        orchestrator._copy_deep_metadata(
            source_path=source,
            output_path=output,
            err_path=err_path,
            cq=45,
            encoder="SVT-AV1 (CPU)",
            original_size=1000,
            finished_at="2025-01-01T00:00:00",
        )

    assert err_path.exists()
    assert "ExifTool metadata copy timed out" in err_path.read_text()


def test_write_vbc_tags_runs_exiftool(tmp_path):
    config = _make_config(use_exif=False, copy_metadata=False)
    orchestrator = Orchestrator(
        config=config,
        event_bus=EventBus(),
        file_scanner=FileScanner([".mp4"], 0),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock(),
    )

    source = tmp_path / "src.mp4"
    source.write_bytes(b"x")
    output = tmp_path / "out.mp4"
    output.write_bytes(b"x")

    with patch("subprocess.run") as mock_run:
        orchestrator._write_vbc_tags(
            source_path=source,
            output_path=output,
            cq=45,
            encoder="SVT-AV1 (CPU)",
            original_size=1000,
            finished_at="2025-01-01T00:00:00",
        )

    assert mock_run.called
