import pytest
from pathlib import Path
from vbc.ui.state import UIState
from vbc.domain.models import VideoFile, CompressionJob, JobStatus

def test_ui_state_initialization():
    state = UIState()
    assert state.completed_count == 0
    assert state.failed_count == 0
    assert state.skipped_count == 0
    assert state.total_input_bytes == 0
    assert state.total_output_bytes == 0
    assert len(state.active_jobs) == 0
    assert len(state.recent_jobs) == 0

def test_ui_state_stats_update():
    state = UIState()
    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, status=JobStatus.COMPLETED, output_path=Path("out.mp4"))
    
    # Simulate completion
    # We'll need a method to update stats from a job
    state.add_completed_job(job, output_size=100)
    
    assert state.completed_count == 1
    assert state.total_input_bytes == 1000
    assert state.total_output_bytes == 100
    assert state.space_saved_bytes == 900
    assert state.compression_ratio == 0.1 # 100/1000

def test_ui_state_active_jobs():
    state = UIState()
    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, status=JobStatus.PROCESSING)
    
    state.add_active_job(job)
    assert len(state.active_jobs) == 1
    assert state.active_jobs[0] == job
    
    state.remove_active_job(job)
    assert len(state.active_jobs) == 0

def test_ui_state_recent_limit():
    state = UIState()
    for i in range(10):
        vf = VideoFile(path=Path(f"test{i}.mp4"), size_bytes=1000)
        job = CompressionJob(source_file=vf, status=JobStatus.COMPLETED)
        state.add_completed_job(job, 500)
        
    # Should only keep 5 most recent
    assert len(state.recent_jobs) == 5
    assert state.recent_jobs[0].source_file.path.name == "test9.mp4"
