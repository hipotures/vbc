import pytest
from pathlib import Path
from unittest.mock import MagicMock
from vbc.pipeline.orchestrator import Orchestrator
from vbc.config.models import AppConfig, GeneralConfig, AutoRotateConfig
from vbc.domain.models import VideoFile, VideoMetadata

def test_dynamic_cq_selection():
    # Config with dynamic CQ
    config = AppConfig(
        general=GeneralConfig(
            threads=1,
            cq=45,
            dynamic_cq={"Sony": 35, "DJI": 30}
        )
    )
    
    # Mock adapters (not used for this unit logic test but needed for init)
    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock()
    )
    
    # File with Sony camera
    vf_sony = VideoFile(
        path=Path("test.mp4"),
        size_bytes=1000,
        metadata=VideoMetadata(width=1920, height=1080, codec="h264", fps=30, camera_model="Sony A7")
    )
    
    # Logic method to be implemented (public or internal)
    # We can test internal method or refactor logic to a standalone class
    # For now testing _determine_cq method on orchestrator
    assert orchestrator._determine_cq(vf_sony) == 35
    
    # File with DJI camera
    vf_dji = VideoFile(
        path=Path("test.mp4"),
        size_bytes=1000,
        metadata=VideoMetadata(width=1920, height=1080, codec="h264", fps=30, camera_model="DJI Osmo")
    )
    assert orchestrator._determine_cq(vf_dji) == 30
    
    # Unknown camera
    vf_other = VideoFile(
        path=Path("test.mp4"),
        size_bytes=1000,
        metadata=VideoMetadata(width=1920, height=1080, codec="h264", fps=30, camera_model="Canon")
    )
    assert orchestrator._determine_cq(vf_other) == 45

def test_auto_rotation_logic():
    config = AppConfig(
        general=GeneralConfig(threads=1),
        autorotate=AutoRotateConfig(patterns={"QVR_.*": 180, "Selfie_.*": 90})
    )
    
    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock()
    )
    
    # Matches pattern
    vf_qvr = VideoFile(path=Path("/tmp/QVR_20251010.mp4"), size_bytes=1000)
    assert orchestrator._determine_rotation(vf_qvr) == 180
    
    # No match
    vf_normal = VideoFile(path=Path("/tmp/Holiday.mp4"), size_bytes=1000)
    assert orchestrator._determine_rotation(vf_normal) is None
