import threading
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from vbc.infrastructure.ffmpeg import FFmpegAdapter
from vbc.domain.models import VideoFile, CompressionJob, JobStatus
from vbc.config.models import GeneralConfig

def test_ffmpeg_command_generation_gpu():
    config = GeneralConfig(threads=4, cq=45, gpu=True)
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))

    adapter = FFmpegAdapter(event_bus=MagicMock())
    cmd = adapter._build_command(job, config)

    assert "ffmpeg" in cmd
    assert "-c:v" in cmd
    assert "av1_nvenc" in cmd
    assert "input.mp4" in cmd
    # FFmpeg writes to .tmp file first, then renames to .mp4
    assert "output.tmp" in cmd or any(".tmp" in str(c) for c in cmd)

def test_ffmpeg_command_generation_cpu():
    config = GeneralConfig(threads=4, cq=45, gpu=False)
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))
    
    adapter = FFmpegAdapter(event_bus=MagicMock())
    cmd = adapter._build_command(job, config)
    
    assert "libsvtav1" in cmd

def test_ffmpeg_rotation():
    config = GeneralConfig(threads=4, cq=45, gpu=True)
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))
    
    adapter = FFmpegAdapter(event_bus=MagicMock())
    # 180 degree rotation
    cmd = adapter._build_command(job, config, rotate=180)
    assert "transpose=2,transpose=2" in cmd

def test_ffmpeg_compress_success():
    config = GeneralConfig(threads=4, cq=45, gpu=True)
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))
    
    with patch("subprocess.Popen") as mock_popen:
        process_instance = mock_popen.return_value
        process_instance.stdout = ["frame= 100 fps=10.0 q=45.0 Lsize= 100kB time=00:00:05.00 bitrate= 100.0kbits/s speed=1.0x"]
        process_instance.wait.return_value = 0
        process_instance.returncode = 0
        
        adapter = FFmpegAdapter(event_bus=MagicMock())
        adapter.compress(job, config)
        
        assert job.status == JobStatus.COMPLETED
        assert mock_popen.called

def test_ffmpeg_compress_failure():
    config = GeneralConfig(threads=4, cq=45, gpu=True)
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))
    
    with patch("subprocess.Popen") as mock_popen:
        process_instance = mock_popen.return_value
        process_instance.stdout = ["Error message from ffmpeg"]
        process_instance.wait.return_value = 1
        process_instance.returncode = 1
        
        bus = MagicMock()
        adapter = FFmpegAdapter(event_bus=bus)
        adapter.compress(job, config)
        
        assert job.status == JobStatus.FAILED
        assert "ffmpeg exited with code 1" in job.error_message
        assert bus.publish.called


def test_ffmpeg_compress_shutdown_event_interrupts(tmp_path):
    config = GeneralConfig(threads=4, cq=45, gpu=True)
    vf = VideoFile(path=tmp_path / "input.mp4", size_bytes=1000)
    vf.path.write_bytes(b"x" * 10)
    job = CompressionJob(source_file=vf, output_path=tmp_path / "output.mp4")

    tmp_output = job.output_path.with_suffix(".tmp")
    tmp_output.write_bytes(b"tmp")

    shutdown_event = threading.Event()
    shutdown_event.set()

    process_instance = MagicMock()
    process_instance.stdout = []
    process_instance.poll.return_value = None
    process_instance.wait.return_value = 0

    with patch("subprocess.Popen", return_value=process_instance):
        adapter = FFmpegAdapter(event_bus=MagicMock())
        adapter.compress(job, config, shutdown_event=shutdown_event)

    assert job.status == JobStatus.INTERRUPTED
    assert not tmp_output.exists()
    assert process_instance.terminate.called


def test_ffmpeg_compress_hw_cap_error(tmp_path):
    config = GeneralConfig(threads=4, cq=45, gpu=True)
    vf = VideoFile(path=tmp_path / "input.mp4", size_bytes=1000)
    vf.path.write_bytes(b"x" * 10)
    job = CompressionJob(source_file=vf, output_path=tmp_path / "output.mp4")

    tmp_output = job.output_path.with_suffix(".tmp")
    tmp_output.write_bytes(b"tmp")

    process_instance = MagicMock()
    process_instance.stdout = ["Hardware is lacking required capabilities"]
    process_instance.poll.return_value = None
    process_instance.wait.return_value = 0
    process_instance.returncode = 0

    bus = MagicMock()
    with patch("subprocess.Popen", return_value=process_instance):
        adapter = FFmpegAdapter(event_bus=bus)
        adapter.compress(job, config)

    assert job.status == JobStatus.HW_CAP_LIMIT
    assert not tmp_output.exists()
    assert bus.publish.called


def test_ffmpeg_compress_color_error_triggers_fix(tmp_path):
    config = GeneralConfig(threads=4, cq=45, gpu=True)
    vf = VideoFile(path=tmp_path / "input.mp4", size_bytes=1000)
    vf.path.write_bytes(b"x" * 10)
    job = CompressionJob(source_file=vf, output_path=tmp_path / "output.mp4")

    process_instance = MagicMock()
    process_instance.stdout = ["is not a valid value for color_primaries"]
    process_instance.poll.return_value = None
    process_instance.wait.return_value = 0
    process_instance.returncode = 0

    with patch("subprocess.Popen", return_value=process_instance):
        adapter = FFmpegAdapter(event_bus=MagicMock())
        adapter._apply_color_fix = MagicMock()
        adapter._apply_color_fix.side_effect = lambda job, config, rotate, shutdown_event=None: setattr(job, "status", JobStatus.COMPLETED)
        adapter.compress(job, config)

    adapter._apply_color_fix.assert_called_once()
    assert job.status == JobStatus.COMPLETED


def test_apply_color_fix_remux_fallback(tmp_path):
    config = GeneralConfig(threads=4, cq=45, gpu=True)
    vf = VideoFile(path=tmp_path / "input.mp4", size_bytes=1000)
    vf.path.write_bytes(b"x" * 10)
    job = CompressionJob(source_file=vf, output_path=tmp_path / "output.mp4")

    colorfix_path = job.output_path.with_name(f"{job.output_path.stem}_colorfix.mp4")
    calls = {"count": 0}
    recorded_path = {"value": None}

    def fake_run(cmd, capture_output=True):
        calls["count"] += 1
        result = MagicMock()
        if calls["count"] == 1:
            result.returncode = 1
        else:
            result.returncode = 0
            colorfix_path.write_bytes(b"fixed")
        return result

    def fake_compress(job, _config, _rotate=None, shutdown_event=None):
        recorded_path["value"] = job.source_file.path
        job.status = JobStatus.COMPLETED

    adapter = FFmpegAdapter(event_bus=MagicMock())
    adapter.compress = MagicMock(side_effect=fake_compress)

    with patch("subprocess.run", side_effect=fake_run):
        adapter._apply_color_fix(job, config, rotate=None)

    assert calls["count"] == 2
    assert recorded_path["value"] == colorfix_path
    assert job.source_file.path == vf.path
    assert not colorfix_path.exists()
    assert job.status == JobStatus.COMPLETED
