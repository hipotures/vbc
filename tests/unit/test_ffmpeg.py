import json
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from vbc.infrastructure.ffmpeg import (
    FFmpegAdapter,
    select_encoder_args,
    apply_cpu_thread_overrides,
)
from vbc.infrastructure.ffprobe import FFprobeAdapter
from vbc.domain.models import VideoFile, CompressionJob, JobStatus
from vbc.domain.models import VideoMetadata
from vbc.config.models import AppConfig, GeneralConfig

def test_ffmpeg_command_generation_gpu():
    config = AppConfig(general=GeneralConfig(threads=4, gpu=True))
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))

    adapter = FFmpegAdapter(event_bus=MagicMock())
    encoder_args = select_encoder_args(config, use_gpu=True)
    cmd = adapter._build_command(job, config, encoder_args, use_gpu=True)

    assert "ffmpeg" in cmd
    assert "-c:v" in cmd
    assert "av1_nvenc" in cmd
    assert "input.mp4" in cmd
    # FFmpeg writes to .tmp file first, then renames to .mp4
    assert "output.tmp" in cmd or any(".tmp" in str(c) for c in cmd)

def test_ffmpeg_command_generation_cpu():
    config = AppConfig(general=GeneralConfig(threads=4, gpu=False))
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))
    
    adapter = FFmpegAdapter(event_bus=MagicMock())
    encoder_args = select_encoder_args(config, use_gpu=False)
    cmd = adapter._build_command(job, config, encoder_args, use_gpu=False)
    
    assert "libsvtav1" in cmd


def test_ffmpeg_command_generation_cpu_threads_limit():
    config = AppConfig(general=GeneralConfig(threads=4, gpu=False, ffmpeg_cpu_threads=4))
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))

    adapter = FFmpegAdapter(event_bus=MagicMock())
    encoder_args = select_encoder_args(config, use_gpu=False)
    encoder_args = apply_cpu_thread_overrides(encoder_args, config.general.ffmpeg_cpu_threads)
    cmd = adapter._build_command(job, config, encoder_args, use_gpu=False)

    threads_index = cmd.index("-threads")
    assert cmd[threads_index + 1] == "4"
    svt_index = cmd.index("-svtav1-params")
    assert "lp=4" in cmd[svt_index + 1]

def test_ffmpeg_rotation():
    config = AppConfig(general=GeneralConfig(threads=4, gpu=True))
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))
    
    adapter = FFmpegAdapter(event_bus=MagicMock())
    # 180 degree rotation
    encoder_args = select_encoder_args(config, use_gpu=True)
    cmd = adapter._build_command(job, config, encoder_args, use_gpu=True, rotate=180)
    assert "transpose=2,transpose=2" in cmd

def test_audio_codec_detection_drives_audio_options():
    mock_output = {
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30/1",
            },
            {
                "index": 1,
                "codec_name": "pcm_s16le",
                "codec_type": "audio",
            },
        ],
        "format": {
            "duration": "10.0"
        }
    }

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = json.dumps(mock_output)
        mock_run.return_value.returncode = 0

        ffprobe = FFprobeAdapter()
        info = ffprobe.get_stream_info(Path("test.mp4"))

    assert info["audio_codec"] == "pcm_s16le"

    metadata = VideoMetadata(
        width=1920,
        height=1080,
        codec="h264",
        audio_codec=info["audio_codec"],
        fps=30.0,
    )
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000, metadata=metadata)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))
    adapter = FFmpegAdapter(event_bus=MagicMock())
    config = AppConfig(general=GeneralConfig(threads=1, gpu=True))

    encoder_args = select_encoder_args(config, use_gpu=True)
    cmd = adapter._build_command(job, config, encoder_args, use_gpu=True)
    audio_index = cmd.index("-c:a")
    assert cmd[audio_index + 1] == "aac"
    assert "-b:a" in cmd
    bitrate_index = cmd.index("-b:a")
    assert cmd[bitrate_index + 1] == "256k"

    job.source_file.metadata.audio_codec = "aac"
    encoder_args = select_encoder_args(config, use_gpu=True)
    cmd = adapter._build_command(job, config, encoder_args, use_gpu=True)
    audio_index = cmd.index("-c:a")
    assert cmd[audio_index + 1] == "copy"
    assert "-b:a" not in cmd


@pytest.mark.parametrize(
    ("audio_codec", "expected_codec", "expected_bitrate"),
    [
        ("flac (24-bit)", "aac", "256k"),
        ("alac", "aac", "256k"),
        ("truehd", "aac", "256k"),
        ("mlp", "aac", "256k"),
        ("wavpack", "aac", "256k"),
        ("ape", "aac", "256k"),
        ("tta", "aac", "256k"),
        ("aac", "copy", None),
        ("mp3", "copy", None),
        ("opus", "aac", "192k"),
        ("no-audio", "aac", "192k"),
        (None, "aac", "192k"),
    ],
)
def test_audio_codec_selection_matrix(audio_codec, expected_codec, expected_bitrate):
    config = AppConfig(general=GeneralConfig(threads=1, gpu=True))
    metadata = VideoMetadata(
        width=1920,
        height=1080,
        codec="h264",
        audio_codec=audio_codec,
        fps=30.0,
    )
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000, metadata=metadata)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))

    adapter = FFmpegAdapter(event_bus=MagicMock())
    encoder_args = select_encoder_args(config, use_gpu=True)
    cmd = adapter._build_command(job, config, encoder_args, use_gpu=True)

    audio_index = cmd.index("-c:a")
    assert cmd[audio_index + 1] == expected_codec
    if expected_bitrate:
        bitrate_index = cmd.index("-b:a")
        assert cmd[bitrate_index + 1] == expected_bitrate
    else:
        assert "-b:a" not in cmd

def test_ffmpeg_compress_success():
    config = AppConfig(general=GeneralConfig(threads=4, gpu=True))
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))
    
    with patch("subprocess.Popen") as mock_popen:
        process_instance = mock_popen.return_value
        process_instance.stdout = ["frame= 100 fps=10.0 q=45.0 Lsize= 100kB time=00:00:05.00 bitrate= 100.0kbits/s speed=1.0x"]
        process_instance.wait.return_value = 0
        process_instance.returncode = 0
        
        adapter = FFmpegAdapter(event_bus=MagicMock())
        adapter.compress(job, config, use_gpu=True)
        
        assert job.status == JobStatus.COMPLETED
        assert mock_popen.called

def test_ffmpeg_compress_failure():
    config = AppConfig(general=GeneralConfig(threads=4, gpu=True))
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))
    
    with patch("subprocess.Popen") as mock_popen:
        process_instance = mock_popen.return_value
        process_instance.stdout = ["Error message from ffmpeg"]
        process_instance.wait.return_value = 1
        process_instance.returncode = 1
        
        bus = MagicMock()
        adapter = FFmpegAdapter(event_bus=bus)
        adapter.compress(job, config, use_gpu=True)
        
        assert job.status == JobStatus.FAILED
        assert "ffmpeg exited with code 1" in job.error_message
        assert bus.publish.called


def test_ffmpeg_compress_shutdown_event_interrupts(tmp_path):
    config = AppConfig(general=GeneralConfig(threads=4, gpu=True))
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
        adapter.compress(job, config, use_gpu=True, shutdown_event=shutdown_event)

    assert job.status == JobStatus.INTERRUPTED
    assert not tmp_output.exists()
    assert process_instance.terminate.called


def test_ffmpeg_compress_hw_cap_error(tmp_path):
    config = AppConfig(general=GeneralConfig(threads=4, gpu=True))
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
        adapter.compress(job, config, use_gpu=True)

    assert job.status == JobStatus.HW_CAP_LIMIT
    assert not tmp_output.exists()
    assert bus.publish.called


def test_ffmpeg_compress_color_error_triggers_fix(tmp_path):
    config = AppConfig(general=GeneralConfig(threads=4, gpu=True))
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
        adapter._apply_color_fix.side_effect = lambda job, config, use_gpu, quality, rotate, shutdown_event=None: setattr(job, "status", JobStatus.COMPLETED)
        adapter.compress(job, config, use_gpu=True)

    adapter._apply_color_fix.assert_called_once()
    assert job.status == JobStatus.COMPLETED


def test_apply_color_fix_remux_fallback(tmp_path):
    config = AppConfig(general=GeneralConfig(threads=4, gpu=True))
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

    def fake_compress(job, _config, _use_gpu, quality=None, rotate=None, shutdown_event=None):
        recorded_path["value"] = job.source_file.path
        job.status = JobStatus.COMPLETED

    adapter = FFmpegAdapter(event_bus=MagicMock())
    adapter.compress = MagicMock(side_effect=fake_compress)

    with patch("subprocess.run", side_effect=fake_run):
        adapter._apply_color_fix(job, config, use_gpu=True, quality=None, rotate=None)

    assert calls["count"] == 2
    assert recorded_path["value"] == colorfix_path
    assert job.source_file.path == vf.path
    assert not colorfix_path.exists()
    assert job.status == JobStatus.COMPLETED
