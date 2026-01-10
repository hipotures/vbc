import pytest
from unittest.mock import MagicMock, patch
from vbc.infrastructure.ffmpeg import FFmpegAdapter
from vbc.domain.models import VideoFile, CompressionJob, JobStatus
from vbc.config.models import AppConfig, GeneralConfig
from vbc.infrastructure.event_bus import EventBus
from vbc.ui.state import UIState
from vbc.ui.manager import UIManager
from pathlib import Path

# This will fail to import if HardwareCapabilityExceeded is not defined yet
try:
    from vbc.domain.events import HardwareCapabilityExceeded
except ImportError:
    HardwareCapabilityExceeded = None

def test_ffmpeg_hw_cap_detection():
    if HardwareCapabilityExceeded is None:
        pytest.fail("HardwareCapabilityExceeded event not defined")

    bus = MagicMock(spec=EventBus)
    adapter = FFmpegAdapter(event_bus=bus)
    
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, output_path=Path("output.mp4"))
    config = AppConfig(general=GeneralConfig(threads=4, gpu=True))
    
    # Mock ffmpeg output containing the error
    hw_error_msg = "Error: Hardware is lacking required capabilities"
    
    with patch("subprocess.Popen") as mock_popen:
        process_instance = mock_popen.return_value
        process_instance.stdout = [hw_error_msg]
        process_instance.wait.return_value = 1
        process_instance.returncode = 1
        
        adapter.compress(job, config, use_gpu=True)
        
        # Verify event was published
        bus.publish.assert_called()
        events = [call[0][0] for call in bus.publish.call_args_list]
        assert any(isinstance(e, HardwareCapabilityExceeded) for e in events)
        assert job.status == JobStatus.HW_CAP_LIMIT

def test_ui_state_hw_cap_update():
    if HardwareCapabilityExceeded is None:
        pytest.fail("HardwareCapabilityExceeded event not defined")

    bus = EventBus()
    state = UIState()
    manager = UIManager(bus, state)
    
    vf = VideoFile(path=Path("input.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, status=JobStatus.HW_CAP_LIMIT)
    
    initial_count = state.hw_cap_count
    bus.publish(HardwareCapabilityExceeded(job=job))
    
    assert state.hw_cap_count == initial_count + 1
