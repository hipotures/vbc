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


def test_ui_state_logs_pagination_and_reset():
    state = UIState()
    assert "logs" in state.OVERLAY_TABS

    for i in range(12):
        vf = VideoFile(path=Path(f"err_{i}.mp4"), size_bytes=1000 + i)
        job = CompressionJob(source_file=vf, status=JobStatus.FAILED)
        state.add_session_error(job, f"error-{i}")

    entries, page_idx, total_pages, total_entries = state.get_logs_page()
    assert len(entries) == 10
    assert page_idx == 0
    assert total_pages == 2
    assert total_entries == 12
    assert entries[0].error_message == "error-11"

    state.cycle_logs_page(1)
    entries, page_idx, total_pages, total_entries = state.get_logs_page()
    assert len(entries) == 2
    assert page_idx == 1
    assert total_pages == 2
    assert total_entries == 12
    assert entries[0].error_message == "error-1"

    # New error should reset paging to newest page.
    state.add_session_error(
        CompressionJob(source_file=VideoFile(path=Path("new.mp4"), size_bytes=50), status=JobStatus.FAILED),
        "new-error",
    )
    entries, page_idx, total_pages, total_entries = state.get_logs_page()
    assert page_idx == 0
    assert total_pages == 2
    assert total_entries == 13
    assert entries[0].error_message == "new-error"


def test_ui_state_discovery_error_deduplicates_by_path_and_message():
    state = UIState()

    state.add_discovery_error(
        path=Path("stale.mp4"),
        size_bytes=123,
        error_message="ffmpeg exited with code 245",
    )
    state.add_discovery_error(
        path=Path("stale.mp4"),
        size_bytes=123,
        error_message="ffmpeg exited with code 245",
    )
    state.add_discovery_error(
        path=Path("stale.mp4"),
        size_bytes=123,
        error_message="different message",
    )

    entries, _page_idx, _total_pages, total_entries = state.get_logs_page()
    assert total_entries == 2
    assert entries[0].error_message == "different message"
    assert entries[1].error_message == "ffmpeg exited with code 245"


def test_ui_state_discovery_error_keeps_failed_entry_with_same_path_and_message():
    state = UIState()

    vf = VideoFile(path=Path("stale.mp4"), size_bytes=123)
    failed_job = CompressionJob(source_file=vf, status=JobStatus.FAILED)
    state.add_session_error(failed_job, "ffmpeg exited with code 245")
    state.add_discovery_error(
        path=Path("stale.mp4"),
        size_bytes=123,
        error_message="ffmpeg exited with code 245",
    )

    entries, _page_idx, _total_pages, total_entries = state.get_logs_page()
    assert total_entries == 2
    assert entries[0].error_message == "ffmpeg exited with code 245"
    assert entries[1].error_message == "ffmpeg exited with code 245"
