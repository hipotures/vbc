import json
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from scripts import restore_failed_manifest as restore_module
from scripts.restore_failed_manifest import (
    RestoreError,
    RestoreResult,
    SourceRestore,
    _render_result,
    restore_failed_manifest,
)


def _manifest(inputs: list[Path], output_path: Path) -> dict:
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
        "inputs": [str(path) for path in inputs],
        "output_path": str(output_path),
        "source_policy": "keep",
        "compression_profile": "tiktok",
        "error_policy": {"missing_input": "fail"},
    }


def _write_config(
    path: Path,
    metadata_dir: Path,
    archive_root: Path,
    *,
    explicit_error_dir: Path | None = None,
) -> None:
    payload = {
        "general": {},
        "input_dirs": [
            {
                "path": str(metadata_dir),
                "enabled": True,
                "metadata": True,
                "watch": True,
            }
        ],
        "suffix_output_dirs": "_out",
        "suffix_errors_dirs": "_err",
        "metadata": {
            "source_policy": "move_all",
            "move_after_success_dir": str(archive_root),
        },
    }
    if explicit_error_dir is not None:
        payload["errors_dirs"] = [str(explicit_error_dir)]
        payload["suffix_errors_dirs"] = None
    path.write_text(json.dumps(payload))


def _setup_failed_task(tmp_path, *, explicit_errors=False):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    error_dir = (
        tmp_path / "custom_errors"
        if explicit_errors
        else tmp_path / "metadata_err"
    )
    error_dir.mkdir()
    archive_root = tmp_path / "sources_compressed"
    archive_user = archive_root / "user"
    archive_user.mkdir(parents=True)
    recordings_user = tmp_path / "recordings" / "user"
    first = recordings_user / "user_20260720_010000_part001.mp4"
    second = recordings_user / "user_20260720_010000_part002.mp4"
    failed_manifest = error_dir / "request.json"
    failed_manifest.write_text(
        json.dumps(_manifest([first, second], tmp_path / "compressed.mp4"))
    )
    error_marker = error_dir / "request.err"
    error_marker.write_text("original verification error")
    config_path = tmp_path / "vbc.yaml"
    _write_config(
        config_path,
        metadata_dir,
        archive_root,
        explicit_error_dir=error_dir if explicit_errors else None,
    )
    return {
        "metadata_dir": metadata_dir,
        "error_dir": error_dir,
        "archive_user": archive_user,
        "first": first,
        "second": second,
        "failed_manifest": failed_manifest,
        "error_marker": error_marker,
        "config_path": config_path,
    }


@pytest.mark.parametrize("explicit_errors", [False, True])
def test_restores_sources_then_manifest_from_configured_error_dir(
    tmp_path,
    explicit_errors,
):
    task = _setup_failed_task(tmp_path, explicit_errors=explicit_errors)
    archived_first = task["archive_user"] / task["first"].name
    archived_second = task["archive_user"] / task["second"].name
    archived_first.write_bytes(b"first")
    archived_second.write_bytes(b"second")

    result = restore_failed_manifest(
        task["failed_manifest"],
        task["config_path"],
    )

    assert task["first"].read_bytes() == b"first"
    assert task["second"].read_bytes() == b"second"
    assert not archived_first.exists()
    assert not archived_second.exists()
    assert not task["failed_manifest"].exists()
    assert (task["metadata_dir"] / "request.json").exists()
    assert task["error_marker"].read_text() == "original verification error"
    assert result.manifest_destination == task["metadata_dir"] / "request.json"
    assert len(result.restored_sources) == 2


def test_missing_archive_source_aborts_without_moving_anything(tmp_path):
    task = _setup_failed_task(tmp_path)
    archived_first = task["archive_user"] / task["first"].name
    archived_first.write_bytes(b"first")

    with pytest.raises(RestoreError, match="missing source files") as exc_info:
        restore_failed_manifest(task["failed_manifest"], task["config_path"])

    assert str(task["second"]) in str(exc_info.value)
    assert archived_first.read_bytes() == b"first"
    assert not task["first"].exists()
    assert task["failed_manifest"].exists()
    assert not (task["metadata_dir"] / "request.json").exists()


def test_dry_run_reports_plan_without_moving_files(tmp_path):
    task = _setup_failed_task(tmp_path)
    archived_first = task["archive_user"] / task["first"].name
    archived_second = task["archive_user"] / task["second"].name
    archived_first.write_bytes(b"first")
    archived_second.write_bytes(b"second")

    result = restore_failed_manifest(
        task["failed_manifest"],
        task["config_path"],
        dry_run=True,
    )

    assert result.dry_run is True
    assert len(result.restored_sources) == 2
    assert archived_first.exists()
    assert archived_second.exists()
    assert task["failed_manifest"].exists()
    assert not task["first"].exists()


def test_existing_original_is_accepted_when_archive_copy_is_absent(tmp_path):
    task = _setup_failed_task(tmp_path)
    task["first"].parent.mkdir(parents=True)
    task["first"].write_bytes(b"already restored")
    task["second"].write_bytes(b"already restored too")

    result = restore_failed_manifest(
        task["failed_manifest"],
        task["config_path"],
    )

    assert result.restored_sources == ()
    assert result.already_present_sources == (task["first"], task["second"])
    assert (task["metadata_dir"] / "request.json").exists()


def test_existing_destination_manifest_aborts_before_source_moves(tmp_path):
    task = _setup_failed_task(tmp_path)
    archived_first = task["archive_user"] / task["first"].name
    archived_second = task["archive_user"] / task["second"].name
    archived_first.write_bytes(b"first")
    archived_second.write_bytes(b"second")
    destination = task["metadata_dir"] / "request.json"
    destination.write_text("existing")

    with pytest.raises(RestoreError, match="destination manifest already exists"):
        restore_failed_manifest(task["failed_manifest"], task["config_path"])

    assert archived_first.exists()
    assert archived_second.exists()
    assert task["failed_manifest"].exists()
    assert destination.read_text() == "existing"


@pytest.mark.parametrize(
    ("dry_run", "action", "summary"),
    [
        (False, "RESTORED", "Recovery complete"),
        (True, "WOULD MOVE", "Plan validated"),
    ],
)
def test_render_result_uses_compact_three_column_table(
    tmp_path,
    dry_run,
    action,
    summary,
):
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=240)
    result = RestoreResult(
        manifest_destination=tmp_path / "metadata" / "request.json",
        restored_sources=(
            SourceRestore(
                tmp_path / "archive" / "part001.mp4",
                tmp_path / "recordings" / "part001.mp4",
            ),
        ),
        already_present_sources=(tmp_path / "recordings" / "part002.mp4",),
        error_marker=tmp_path / "metadata_err" / "request.err",
        dry_run=dry_run,
    )

    _render_result(result, console)

    rendered = output.getvalue()
    assert "Status" in rendered
    assert "Item" in rendered
    assert "Path" in rendered
    assert action in rendered
    assert "PRESENT" in rendered
    assert "RETAINED" in rendered
    assert summary in rendered
    assert "2 source(s) ready" in rendered


def test_source_move_failure_rolls_back_earlier_sources(tmp_path, monkeypatch):
    task = _setup_failed_task(tmp_path)
    archived_first = task["archive_user"] / task["first"].name
    archived_second = task["archive_user"] / task["second"].name
    archived_first.write_bytes(b"first")
    archived_second.write_bytes(b"second")
    real_move = restore_module.shutil.move

    def fail_second_source(source, destination):
        if Path(source) == archived_second:
            raise OSError("simulated move failure")
        return real_move(source, destination)

    monkeypatch.setattr(restore_module.shutil, "move", fail_second_source)

    with pytest.raises(RestoreError, match="simulated move failure"):
        restore_failed_manifest(task["failed_manifest"], task["config_path"])

    assert archived_first.read_bytes() == b"first"
    assert archived_second.read_bytes() == b"second"
    assert not task["first"].exists()
    assert not task["second"].exists()
    assert task["failed_manifest"].exists()
