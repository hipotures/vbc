from datetime import datetime, timedelta
from pathlib import Path

import pytest
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.console import Console

from vbc.domain.models import CompressionJob, JobStatus, VideoFile, VideoMetadata
from vbc.ui.state import UIState
from vbc.ui import dashboard as dashboard_module
from vbc.ui.dashboard import Dashboard

def test_dashboard_initialization():
    """Test that Dashboard can be initialized with UIState."""
    state = UIState()
    dashboard = Dashboard(state, panel_height_scale=0.7, max_active_jobs=8)
    assert dashboard.state is state

def test_dashboard_context_manager():
    """Test that Dashboard can be used as context manager."""
    state = UIState()
    dashboard = Dashboard(state, panel_height_scale=0.7, max_active_jobs=8)
    # Dashboard should have __enter__ and __exit__ for context manager protocol
    assert hasattr(dashboard, '__enter__')
    assert hasattr(dashboard, '__exit__')


def test_dashboard_format_helpers():
    state = UIState()
    dashboard = Dashboard(state, panel_height_scale=0.7, max_active_jobs=8)

    assert dashboard.format_size(0) == "0B"
    assert dashboard.format_size(1024) == "1.0KB"
    assert dashboard.format_time(59) == "59s"
    assert dashboard.format_time(61) == "01m 01s"
    assert dashboard.format_time(3661) == "1h 01m"

    metadata = VideoMetadata(width=1920, height=1080, codec="h264", fps=29.97)
    assert dashboard.format_resolution(metadata) == "2M"
    assert dashboard.format_fps(metadata) == "29fps"

    state.strip_unicode_display = True
    assert dashboard._sanitize_filename("cafe\u00e9") == "cafe"
    assert dashboard._sanitize_filename("\U0001F3A5 2023-12-09") == "2023-12-09"
    state.strip_unicode_display = False
    assert dashboard._sanitize_filename("cafe\u00e9") == "cafe\u00e9"

    assert dashboard._format_quality_display_for_ui("44.758 Mbps") == "45Mbps"
    assert dashboard._format_quality_display_for_ui("200 Mbps") == "200Mbps"
    assert dashboard._format_quality_display_for_ui("CQ35") == "cq35"
    assert dashboard._format_quality_display_for_ui("CRF32") == "crf32"

    vf = VideoFile(path=Path("sample.mp4"), size_bytes=1024, metadata=metadata)
    cq_job = CompressionJob(source_file=vf, status=JobStatus.PROCESSING, quality_display="CQ40")
    rate_job = CompressionJob(source_file=vf, status=JobStatus.PROCESSING, quality_display="95 Mbps")
    assert dashboard._active_quality_meta_suffix(cq_job) == " → cq40"
    assert dashboard._active_quality_meta_suffix(rate_job) == " → 95Mbps"


def test_dashboard_panels_with_state(tmp_path):
    state = UIState()
    state.completed_count = 2
    state.failed_count = 1
    state.skipped_count = 1
    state.hw_cap_count = 1
    state.cam_skipped_count = 1
    state.min_ratio_skip_count = 1
    state.discovery_finished = True
    state.files_to_process = 4
    state.already_compressed_count = 1
    state.ignored_small_count = 1
    state.ignored_err_count = 1
    state.ignored_av1_count = 1
    state.processing_start_time = datetime.now() - timedelta(seconds=10)
    state.total_input_bytes = 10 * 1024 * 1024

    dashboard = Dashboard(state, panel_height_scale=0.7, max_active_jobs=8)

    # Test top bar (was status_panel)
    top_bar = dashboard._generate_top_bar()
    assert isinstance(top_bar, Panel)

    # Test progress panel (requires h_lines parameter)
    progress_panel = dashboard._generate_progress(h_lines=10)
    assert isinstance(progress_panel, Panel)

    source = tmp_path / "video.mp4"
    source.write_bytes(b"x" * 100)
    vf = VideoFile(path=source, size_bytes=source.stat().st_size, metadata=VideoMetadata(width=1280, height=720, codec="h264", fps=30.0))
    job = CompressionJob(source_file=vf, status=JobStatus.PROCESSING, rotation_angle=180)
    state.active_jobs = [job]
    state.job_start_times[vf.path.name] = datetime.now() - timedelta(seconds=5)

    # Test active jobs panel (was processing_panel, requires h_lines parameter)
    active_jobs_panel = dashboard._generate_active_jobs_panel(h_lines=10)
    assert isinstance(active_jobs_panel, Panel)

    completed_job = CompressionJob(source_file=vf, status=JobStatus.COMPLETED, output_path=tmp_path / "out.mp4")
    completed_job.output_size_bytes = 90
    completed_job.duration_seconds = 2.0
    completed_job.error_message = "Ratio 0.95 above threshold, kept original"
    state.recent_jobs.appendleft(completed_job)

    # Test activity panel (was recent_panel, requires h_lines parameter)
    activity_panel = dashboard._generate_activity_panel(h_lines=10)
    assert isinstance(activity_panel, Panel)

    state.pending_files = [vf]
    # Test queue panel (requires h_lines parameter)
    queue_panel = dashboard._generate_queue_panel(h_lines=10)
    assert isinstance(queue_panel, Panel)

    # Test footer (was summary_panel)
    footer = dashboard._generate_footer()
    # Footer returns RenderableType, not necessarily Panel


def test_dashboard_activity_item_shows_heavy_checkmark_when_verified(tmp_path):
    state = UIState()
    dashboard = Dashboard(state, panel_height_scale=0.7, max_active_jobs=8)

    source = tmp_path / "video.mp4"
    source.write_bytes(b"x" * 100)
    vf = VideoFile(path=source, size_bytes=source.stat().st_size)
    completed_job = CompressionJob(source_file=vf, status=JobStatus.COMPLETED)
    completed_job.output_size_bytes = 90
    completed_job.duration_seconds = 2.0
    completed_job.verification_passed = True

    renderable = dashboard._render_activity_item(completed_job, "A")
    console = Console(width=120, record=True)
    console.print(renderable)
    rendered = console.export_text()

    assert "✔" in rendered


def test_dashboard_create_display_overlay():
    state = UIState()
    state.show_overlay = True
    state.active_tab = "settings"
    state.config_lines = ["Threads: 2", "Encoder: SVT-AV1 (CPU)"]
    dashboard = Dashboard(state, panel_height_scale=0.7, max_active_jobs=8)

    display = dashboard.create_display()
    assert isinstance(display, dashboard_module._Overlay)

    state.show_overlay = False
    display = dashboard.create_display()
    assert isinstance(display, Layout)


def test_dashboard_logs_tab_with_pagination(tmp_path):
    state = UIState()
    state.show_overlay = True
    state.active_tab = "logs"
    dashboard = Dashboard(state, panel_height_scale=0.7, max_active_jobs=8)

    for i in range(11):
        vf = VideoFile(path=tmp_path / f"err_{i}.mp4", size_bytes=100 + i)
        job = CompressionJob(source_file=vf, status=JobStatus.FAILED)
        state.add_session_error(job, f"failure-{i}")

    display = dashboard.create_display()
    assert isinstance(display, dashboard_module._Overlay)

    logs_content = dashboard._render_logs_content()
    console = Console(width=120, record=True)
    console.print(logs_content)
    rendered = console.export_text()
    assert "Page 1/2" in rendered
    assert "Prev [" in rendered
    assert "] Next" in rendered


def test_dashboard_create_display_info_overlay():
    state = UIState()
    state.show_info = True
    state.info_message = "No files to process."
    dashboard = Dashboard(state, panel_height_scale=0.7, max_active_jobs=8)

    display = dashboard.create_display()
    assert isinstance(display, dashboard_module._Overlay)
    assert "NOTICE" in str(display.overlay.title)


def test_dashboard_start_stop(monkeypatch):
    state = UIState()
    dashboard = Dashboard(state, panel_height_scale=0.7, max_active_jobs=8)

    class DummyLive:
        def __init__(self, *_args, **_kwargs):
            self.started = False
            self.stopped = False
            self.updated = []

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def update(self, display):
            self.updated.append(display)

    def fake_refresh_loop(self):
        self._stop_refresh.set()

    monkeypatch.setattr(dashboard_module, "Live", DummyLive)
    monkeypatch.setattr(Dashboard, "_refresh_loop", fake_refresh_loop)

    dashboard.start()
    assert isinstance(dashboard._live, DummyLive)
    assert dashboard._live.started
    dashboard.stop()
    assert dashboard._live.stopped
