import os
from datetime import datetime

from scripts import repair_output_mtimes as repair


def _local_timestamp_ns(value: str) -> int:
    parsed = datetime.strptime(value, "%Y%m%d_%H%M%S").astimezone()
    return int(parsed.timestamp()) * 1_000_000_000


def test_repairs_any_matching_files_and_sets_each_direct_parent_to_newest(tmp_path):
    first_dir = tmp_path / "compressed" / "first"
    second_dir = tmp_path / "compressed" / "second"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    older = first_dir / "user_20260720_142322.mp4"
    newer = first_dir / "user_20260720_153045"
    other_extension = second_dir / "prefix_20260102_030405.metadata"
    ignored = first_dir / "no_timestamp.txt"
    for path in (older, newer, other_extension, ignored):
        path.write_bytes(path.name.encode())
    ignored_mtime_ns = ignored.stat().st_mtime_ns

    result = repair.repair_output_mtimes(tmp_path / "compressed")

    assert older.stat().st_mtime_ns == _local_timestamp_ns("20260720_142322")
    assert newer.stat().st_mtime_ns == _local_timestamp_ns("20260720_153045")
    assert other_extension.stat().st_mtime_ns == _local_timestamp_ns("20260102_030405")
    assert ignored.stat().st_mtime_ns == ignored_mtime_ns
    assert first_dir.stat().st_mtime_ns == _local_timestamp_ns("20260720_153045")
    assert second_dir.stat().st_mtime_ns == _local_timestamp_ns("20260102_030405")
    assert result.files_scanned == 4
    assert result.matching_files == 3
    assert result.files_updated == 3
    assert result.directories_considered == 2
    assert result.directories_updated == 2


def test_accepts_numbered_suffix_and_ignores_invalid_dates(tmp_path):
    output_dir = tmp_path / "user"
    output_dir.mkdir()
    numbered = output_dir / "user_20260720_142322_1.mp4"
    invalid = output_dir / "user_20261340_256199.mp4"
    numbered.write_bytes(b"video")
    invalid.write_bytes(b"invalid")
    invalid_mtime_ns = invalid.stat().st_mtime_ns

    result = repair.repair_output_mtimes(tmp_path)

    assert numbered.stat().st_mtime_ns == _local_timestamp_ns("20260720_142322")
    assert invalid.stat().st_mtime_ns == invalid_mtime_ns
    assert result.matching_files == 1
    assert result.invalid_dates == 1


def test_dry_run_reports_changes_without_touching_timestamps(tmp_path):
    output_dir = tmp_path / "user"
    output_dir.mkdir()
    output = output_dir / "recording_20260720_142322.anything"
    output.write_bytes(b"data")
    os.utime(output_dir, ns=(1, 1))
    original_file_mtime_ns = output.stat().st_mtime_ns
    original_dir_mtime_ns = output_dir.stat().st_mtime_ns

    result = repair.repair_output_mtimes(tmp_path, dry_run=True)

    assert result.files_updated == 1
    assert result.directories_updated == 1
    assert output.stat().st_mtime_ns == original_file_mtime_ns
    assert output_dir.stat().st_mtime_ns == original_dir_mtime_ns
