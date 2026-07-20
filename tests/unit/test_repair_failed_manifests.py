import json
from io import StringIO
from pathlib import Path

from rich.console import Console

from scripts.repair_failed_manifests import (
    MAX_FFMPEG_MEMORY_BYTES,
    _render_summary,
    collect_candidates,
    repair_candidate,
)


def _manifest(source: Path, output: Path) -> dict:
    return {
        "schema_version": 1,
        "request_id": "ttracker-user_20260720_010000",
        "created_at": "2026-07-20T01:10:00+02:00",
        "producer": {
            "app": "ttracker",
            "username": "user",
            "recording_id": "user_20260720_010000",
            "source_size_bytes": 100,
            "source_latest_mtime_ns": 123,
        },
        "operation": "concat_transcode",
        "inputs": [str(source)],
        "output_path": str(output),
        "source_policy": "move_all",
        "compression_profile": "tiktok",
        "error_policy": {"missing_input": "fail"},
    }


def _failed_task(tmp_path: Path, error_text: str):
    metadata_dir = tmp_path / "metadata"
    error_dir = tmp_path / "metadata_err"
    archive_root = tmp_path / "archive"
    archive_user = archive_root / "user"
    metadata_dir.mkdir(parents=True)
    error_dir.mkdir(parents=True)
    archive_user.mkdir(parents=True)
    source = tmp_path / "recordings" / "user" / "part001.mp4"
    archived_source = archive_user / source.name
    archived_source.write_bytes(b"video")
    manifest_path = error_dir / "request.json"
    manifest_path.write_text(
        json.dumps(_manifest(source, tmp_path / "compressed" / "output.mp4"))
    )
    error_path = manifest_path.with_suffix(".err")
    error_path.write_text(error_text)
    config_path = tmp_path / "vbc.yaml"
    config_path.write_text(
        json.dumps(
            {
                "general": {"min_size_bytes": 0},
                "input_dirs": [
                    {
                        "path": str(metadata_dir),
                        "enabled": True,
                        "metadata": True,
                    }
                ],
                "suffix_output_dirs": "_out",
                "suffix_errors_dirs": "_err",
                "metadata": {
                    "source_policy": "move_all",
                    "move_after_success_dir": str(archive_root),
                },
            }
        )
    )
    return manifest_path, error_path, config_path, archived_source, source


def test_directory_scan_classifies_supported_and_unsupported_errors(tmp_path):
    supported_json, _, _, _, _ = _failed_task(
        tmp_path / "supported",
        "ffmpeg exited with code 244",
    )
    unsupported_root = tmp_path / "unsupported"
    unsupported_json, unsupported_err, _, _, _ = _failed_task(
        unsupported_root,
        "ffmpeg exited with code 234",
    )
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    for source in (
        supported_json,
        supported_json.with_suffix(".err"),
        unsupported_json,
        unsupported_err,
    ):
        destination = scan_dir / f"{source.parent.parent.name}-{source.name}"
        destination.write_bytes(source.read_bytes())

    candidates = collect_candidates(scan_dir)

    assert len(candidates) == 2
    assert sum(candidate.handler is not None for candidate in candidates) == 1
    assert sum(candidate.handler is None for candidate in candidates) == 1


def test_error_number_minus_12_selects_drop_audio_handler(tmp_path):
    manifest_path, _, _, _, _ = _failed_task(
        tmp_path,
        "Error number: -12 (Cannot allocate memory)",
    )

    candidate = collect_candidates(manifest_path)[0]

    assert candidate.handler is not None
    assert candidate.handler.drop_audio is True


def test_dry_run_validates_plan_without_moving_sources(tmp_path):
    manifest_path, _, config_path, archived_source, source = _failed_task(
        tmp_path,
        "ffmpeg exited with code 244",
    )
    candidate = collect_candidates(manifest_path)[0]
    console = Console(file=StringIO(), force_terminal=False)

    outcome = repair_candidate(
        candidate,
        config_path,
        console,
        dry_run=True,
    )

    assert outcome.status == "READY"
    assert archived_source.exists()
    assert not source.exists()
    assert manifest_path.exists()


def test_rich_summary_is_compact_and_reports_memory_cap(tmp_path):
    manifest_path, _, config_path, _, _ = _failed_task(
        tmp_path,
        "ffmpeg exited with code 244",
    )
    candidate = collect_candidates(manifest_path)[0]
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=180)
    outcome = repair_candidate(candidate, config_path, console, dry_run=True)

    _render_summary([outcome], console, dry_run=True)

    rendered = output.getvalue()
    assert "Status" in rendered
    assert "Manifest" in rendered
    assert "Detail" in rendered
    assert "READY" in rendered
    assert "RAM cap=50 GiB" in rendered
    assert MAX_FFMPEG_MEMORY_BYTES == 50 * 1024**3
