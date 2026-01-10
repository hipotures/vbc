import pytest
from unittest.mock import MagicMock, patch
from vbc.infrastructure.ffmpeg import FFmpegAdapter
from vbc.domain.models import VideoFile, CompressionJob, JobStatus
from vbc.config.models import AppConfig, GeneralConfig
from pathlib import Path

def test_ffmpeg_detects_color_error():
    bus = MagicMock()
    adapter = FFmpegAdapter(event_bus=bus)
    
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))
    config = AppConfig(general=GeneralConfig(threads=1, gpu=True))
    
    # Error message that triggers color fix
    color_error_msg = "Error: is not a valid value for color_primaries"
    
    with patch("subprocess.Popen") as mock_popen:
        process_instance = mock_popen.return_value
        process_instance.stdout = [color_error_msg]
        process_instance.wait.return_value = 1
        process_instance.returncode = 1
        
        # We need to mock build_command to check what was called during retry
        # but for now let's just see if it attempts a second Popen call
        with patch.object(adapter, '_apply_color_fix') as mock_fix:
            adapter.compress(job, config, use_gpu=True)
            assert mock_fix.called
