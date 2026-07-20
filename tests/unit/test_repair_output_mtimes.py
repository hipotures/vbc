import json
import os

from scripts import repair_output_mtimes as repair


def _manifest(output_path, source_mtime_ns):
    return {
        "schema_version": 1,
        "request_id": f"request-{source_mtime_ns}",
        "created_at": "2026-07-18T12:10:00+02:00",
        "producer": {
            "app": "ttracker",
            "username": "user",
            "recording_id": f"user-{source_mtime_ns}",
            "source_size_bytes": 100,
            "source_latest_mtime_ns": source_mtime_ns,
        },
        "operation": "concat_transcode",
        "inputs": [str(output_path.parent / "source.mp4")],
        "output_path": str(output_path),
        "source_policy": "keep",
        "compression_profile": "tiktok",
        "error_policy": {"missing_input": "fail"},
    }


def test_repairs_base_and_tagged_numbered_outputs_but_ignores_backup(
    tmp_path,
    monkeypatch,
):
    metadata_out = tmp_path / "metadata_out"
    metadata_out.mkdir()
    output_dir = tmp_path / "compressed" / "user"
    output_dir.mkdir(parents=True)
    base = output_dir / "recording.mp4"
    split = output_dir / "recording_2.mp4"
    untagged_backup = output_dir / "recording_1.mp4"
    for path in (base, split, untagged_backup):
        path.write_bytes(path.name.encode())
    target_ns = 2_000_000_000_123_456_789
    manifest = metadata_out / "request.json"
    manifest.write_text(json.dumps(_manifest(base, target_ns)))
    os.utime(output_dir, ns=(1, 1))
    backup_mtime_ns = untagged_backup.stat().st_mtime_ns
    monkeypatch.setattr(
        repair,
        "_find_vbc_tagged_outputs",
        lambda _paths: {base.resolve(), split.resolve()},
    )

    result = repair.repair_output_mtimes(metadata_out)

    assert base.stat().st_mtime_ns == target_ns
    assert split.stat().st_mtime_ns == target_ns
    assert untagged_backup.stat().st_mtime_ns == backup_mtime_ns
    assert output_dir.stat().st_mtime_ns == 1
    assert result.output_files_considered == 2
    assert result.output_files_updated == 2
    assert result.untagged_outputs_ignored == 1


def test_dry_run_reports_changes_without_touching_timestamps(tmp_path, monkeypatch):
    metadata_out = tmp_path / "metadata_out"
    metadata_out.mkdir()
    output_dir = tmp_path / "compressed" / "user"
    output_dir.mkdir(parents=True)
    output = output_dir / "recording.mp4"
    output.write_bytes(b"video")
    target_ns = 2_000_000_000_123_456_789
    (metadata_out / "request.json").write_text(
        json.dumps(_manifest(output, target_ns))
    )
    original_file_mtime_ns = output.stat().st_mtime_ns
    original_dir_mtime_ns = output_dir.stat().st_mtime_ns
    monkeypatch.setattr(
        repair,
        "_find_vbc_tagged_outputs",
        lambda _paths: {output.resolve()},
    )

    result = repair.repair_output_mtimes(metadata_out, dry_run=True)

    assert result.output_files_updated == 1
    assert output.stat().st_mtime_ns == original_file_mtime_ns
    assert output_dir.stat().st_mtime_ns == original_dir_mtime_ns
