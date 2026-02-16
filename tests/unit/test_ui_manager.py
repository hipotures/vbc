from pathlib import Path

from vbc.infrastructure.event_bus import EventBus
from vbc.ui.state import UIState
from vbc.ui.manager import UIManager
from vbc.domain.events import (
    ActionMessage,
    DiscoveryFinished,
    DiscoveryStarted,
    HardwareCapabilityExceeded,
    InterruptRequested,
    JobCompleted,
    JobFailed,
    JobProgressUpdated,
    JobStarted,
    ProcessingFinished,
    QueueUpdated,
    RequestShutdown,
    ThreadControlEvent,
)
from vbc.domain.models import VideoFile, CompressionJob, JobStatus
from vbc.ui.keyboard import (
    CycleLogsPage,
    ToggleOverlayTab,
    CloseOverlay,
)

def test_ui_manager_updates_state_on_event(tmp_path):
    bus = EventBus()
    state = UIState()
    manager = UIManager(bus, state)
    
    vf = VideoFile(path=Path("test.mp4"), size_bytes=1000)
    job = CompressionJob(source_file=vf, status=JobStatus.PROCESSING)
    
    # 1. Start event
    bus.publish(JobStarted(job=job))
    assert len(state.active_jobs) == 1
    
    # 2. Complete event
    # Mock file for size calculation
    out_file = tmp_path / "out.mp4"
    out_file.write_text("a" * 100) # 100 bytes
    job.output_path = out_file
    
    bus.publish(JobCompleted(job=job))
    assert len(state.active_jobs) == 0
    assert state.completed_count == 1
    assert state.total_output_bytes == 100


def test_ui_manager_discovery_and_controls():
    bus = EventBus()
    state = UIState()
    UIManager(bus, state)

    state.discovery_finished = True
    bus.publish(DiscoveryStarted(directory=Path("input")))
    assert state.discovery_finished is False

    bus.publish(
        DiscoveryFinished(
            files_found=10,
            files_to_process=5,
            already_compressed=2,
            ignored_small=1,
            ignored_err=1,
            ignored_av1=1,
        )
    )
    assert state.total_files_found == 10
    assert state.files_to_process == 5
    assert state.already_compressed_count == 2
    assert state.ignored_small_count == 1
    assert state.ignored_err_count == 1
    assert state.ignored_av1_count == 1
    assert state.discovery_finished is True

    state.current_threads = 1
    bus.publish(ThreadControlEvent(change=-5))
    assert state.current_threads == 1

    state.current_threads = 8
    bus.publish(ThreadControlEvent(change=2))
    assert state.current_threads == 8

    bus.publish(RequestShutdown())
    assert state.shutdown_requested is True

    bus.publish(InterruptRequested())
    assert state.interrupt_requested is True

    bus.publish(ToggleOverlayTab(tab="settings"))
    assert state.show_overlay is True
    assert state.active_tab == "settings"
    bus.publish(ToggleOverlayTab(tab="logs"))
    assert state.active_tab == "logs"
    bus.publish(CycleLogsPage(direction=1))
    assert state.logs_page_index == 0
    bus.publish(CloseOverlay())
    assert state.show_overlay is False


def test_ui_manager_job_lifecycle_updates(tmp_path):
    bus = EventBus()
    state = UIState()
    UIManager(bus, state)

    source = tmp_path / "input.mp4"
    source.write_bytes(b"x" * 100)
    output = tmp_path / "output.mp4"
    output.write_bytes(b"x" * 50)

    vf = VideoFile(path=source, size_bytes=source.stat().st_size)
    job = CompressionJob(source_file=vf, status=JobStatus.PROCESSING, output_path=output)

    bus.publish(JobStarted(job=job))
    assert state.processing_start_time is not None
    assert len(state.active_jobs) == 1

    job.error_message = "Ratio 0.95 above threshold, kept original"
    bus.publish(JobCompleted(job=job))

    assert len(state.active_jobs) == 0
    assert state.completed_count == 1
    assert state.total_output_bytes == output.stat().st_size
    assert state.min_ratio_skip_count == 1
    assert job.duration_seconds is not None


def test_ui_manager_job_failed_av1_skip(tmp_path):
    bus = EventBus()
    state = UIState()
    UIManager(bus, state)

    vf = VideoFile(path=tmp_path / "input.mp4", size_bytes=100)
    job = CompressionJob(source_file=vf, status=JobStatus.SKIPPED)
    state.add_active_job(job)

    bus.publish(JobFailed(job=job, error_message="Already encoded in AV1"))

    assert state.ignored_av1_count == 1
    assert state.failed_count == 0
    assert len(state.session_error_logs) == 1
    assert len(state.active_jobs) == 0


def test_ui_manager_job_failed_camera_skip(tmp_path):
    bus = EventBus()
    state = UIState()
    UIManager(bus, state)

    vf = VideoFile(path=tmp_path / "input.mp4", size_bytes=100)
    job = CompressionJob(source_file=vf, status=JobStatus.SKIPPED)
    state.add_active_job(job)

    bus.publish(JobFailed(job=job, error_message='Camera model "X" not in filter'))

    assert state.cam_skipped_count == 1
    assert state.failed_count == 0
    assert len(state.session_error_logs) == 1
    assert len(state.active_jobs) == 0


def test_ui_manager_job_failed_interrupted(tmp_path):
    bus = EventBus()
    state = UIState()
    UIManager(bus, state)

    vf = VideoFile(path=tmp_path / "input.mp4", size_bytes=100)
    job = CompressionJob(source_file=vf, status=JobStatus.INTERRUPTED)
    state.add_active_job(job)

    bus.publish(JobFailed(job=job, error_message="Interrupted"))

    assert state.interrupted_count == 1
    assert len(state.session_error_logs) == 1
    assert len(state.recent_jobs) == 1
    assert len(state.active_jobs) == 0


def test_ui_manager_job_failed_generic(tmp_path):
    bus = EventBus()
    state = UIState()
    UIManager(bus, state)

    vf = VideoFile(path=tmp_path / "input.mp4", size_bytes=100)
    job = CompressionJob(source_file=vf, status=JobStatus.FAILED)
    state.add_active_job(job)

    bus.publish(JobFailed(job=job, error_message="Something failed"))

    assert state.failed_count == 1
    assert len(state.session_error_logs) == 1
    assert state.session_error_logs[0].error_message == "Something failed"
    assert len(state.recent_jobs) == 1
    assert len(state.active_jobs) == 0


def test_ui_manager_hw_cap_exceeded(tmp_path):
    bus = EventBus()
    state = UIState()
    UIManager(bus, state)

    vf = VideoFile(path=tmp_path / "input.mp4", size_bytes=100)
    job = CompressionJob(source_file=vf)
    state.add_active_job(job)

    bus.publish(HardwareCapabilityExceeded(job=job))

    assert state.hw_cap_count == 1
    assert len(state.active_jobs) == 0


def test_ui_manager_queue_action_and_finish(tmp_path):
    bus = EventBus()
    state = UIState()
    UIManager(bus, state)

    vf = VideoFile(path=tmp_path / "input.mp4", size_bytes=100)
    bus.publish(QueueUpdated(pending_files=[vf]))
    assert state.pending_files == [vf]

    bus.publish(ActionMessage(message="REFRESH requested"))
    assert state.get_last_action() == "REFRESH requested"

    bus.publish(JobProgressUpdated(job=CompressionJob(source_file=vf), progress_percent=10.0))

    bus.publish(ProcessingFinished())
    assert state.finished is True


def test_ui_manager_cycle_logs_page_only_when_logs_tab_active(tmp_path):
    bus = EventBus()
    state = UIState()
    UIManager(bus, state)

    for i in range(12):
        vf = VideoFile(path=tmp_path / f"input_{i}.mp4", size_bytes=100 + i)
        job = CompressionJob(source_file=vf, status=JobStatus.FAILED)
        bus.publish(JobFailed(job=job, error_message=f"error-{i}"))

    assert state.logs_page_index == 0

    # Ignored while overlay is closed.
    bus.publish(CycleLogsPage(direction=1))
    assert state.logs_page_index == 0

    bus.publish(ToggleOverlayTab(tab="settings"))
    bus.publish(CycleLogsPage(direction=1))
    assert state.logs_page_index == 0

    bus.publish(ToggleOverlayTab(tab="logs"))
    bus.publish(CycleLogsPage(direction=1))
    assert state.logs_page_index == 1
