from pathlib import Path
from types import SimpleNamespace

from vbc.infrastructure import web_server


def _recent_job(name: str) -> SimpleNamespace:
    source_file = SimpleNamespace(path=Path(name), size_bytes=1_000_000)
    return SimpleNamespace(
        source_file=source_file,
        status="COMPLETED",
        output_size_bytes=400_000,
        duration_seconds=12.0,
        config_source=SimpleNamespace(value="G"),
        quality_display=None,
        quality_value=None,
        error_message=None,
        verification_passed=True,
    )


def _queued_file(name: str) -> SimpleNamespace:
    metadata = SimpleNamespace(fps=29.97)
    return SimpleNamespace(path=Path(name), size_bytes=500_000, metadata=metadata)


def test_vm_activity_respects_dynamic_max_items():
    stats = {"recent_jobs": [_recent_job(f"video-{idx}.mp4") for idx in range(6)]}

    vm = web_server._vm_activity(stats, max_items=3)

    assert len(vm["jobs"]) == 3
    assert [job["fname"] for job in vm["jobs"]] == ["video-0.mp4", "video-1.mp4", "video-2.mp4"]


def test_vm_activity_prefers_web_recent_jobs_when_available():
    stats = {
        "recent_jobs": [_recent_job("tui-only.mp4")],
        "web_recent_jobs": [_recent_job(f"web-{idx}.mp4") for idx in range(4)],
    }

    vm = web_server._vm_activity(stats, max_items=3)

    assert [job["fname"] for job in vm["jobs"]] == ["web-0.mp4", "web-1.mp4", "web-2.mp4"]


def test_vm_queue_respects_dynamic_max_items_and_more_counter():
    stats = {"pending_files": [_queued_file(f"queue-{idx}.mp4") for idx in range(8)]}

    vm = web_server._vm_queue(stats, max_items=4)

    assert vm["title"] == "QUEUE (8 files)"
    assert len(vm["items"]) == 4
    assert vm["more"] == 4


def test_parse_max_items_param_uses_default_and_clamps():
    assert web_server._parse_max_items_param({}, default=5) == 5
    assert web_server._parse_max_items_param({"max_items": ["abc"]}, default=5) == 5
    assert web_server._parse_max_items_param({"max_items": ["0"]}, default=5) == 1
    assert (
        web_server._parse_max_items_param({"max_items": ["999"]}, default=5)
        == web_server._WEB_MAX_ITEMS
    )
