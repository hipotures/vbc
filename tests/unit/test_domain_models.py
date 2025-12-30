import pytest
from pathlib import Path
from pydantic import ValidationError
from vbc.domain.models import VideoFile, VideoMetadata, CompressionJob, JobStatus

def test_video_metadata_valid():
    meta = VideoMetadata(width=1920, height=1080, codec="h264", fps=30.0)
    assert meta.width == 1920
    assert meta.fps == 30.0

def test_video_file_valid():
    vf = VideoFile(path=Path("test.mp4"), size_bytes=1024)
    assert vf.path == Path("test.mp4")
    assert vf.metadata is None

def test_compression_job_status_flow():
    vf = VideoFile(path=Path("test.mp4"), size_bytes=1024)
    job = CompressionJob(source_file=vf)
    assert job.status == JobStatus.PENDING
    
    job.status = JobStatus.PROCESSING
    assert job.status == JobStatus.PROCESSING
    
    job.status = JobStatus.COMPLETED
    assert job.status == JobStatus.COMPLETED

def test_invalid_status():
    vf = VideoFile(path=Path("test.mp4"), size_bytes=1024)
    with pytest.raises(ValidationError):
        # status must be a JobStatus enum
        CompressionJob(source_file=vf, status="INVALID")
