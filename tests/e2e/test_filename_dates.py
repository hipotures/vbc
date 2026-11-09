"""Filename timestamp repair through the standalone script and real files."""

from __future__ import annotations

import os
import subprocess
import sys

from conftest import REPO


def test_filename_dates_use_local_time_and_preserve_files(tmp_path):
    root = tmp_path / "videos"
    nested = root / "sub folder"
    nested.mkdir(parents=True)
    winter = root / "Screen_Recording_20261027_203457_TikTok.mp4"
    summer = nested / "clip_20260801_120000.mp4"
    invalid = root / "clip_20260230_120000.mp4"
    clock_gap = root / "clip_20260329_023000.mp4"
    unrelated = root / "notes.txt"
    outside = tmp_path / "outside_20261027_203457.mp4"
    paths = (winter, summer, invalid, clock_gap, unrelated, outside)
    atime_ns = 1_600_000_000_123_456_789
    mtime_ns = 1_700_000_000_987_654_321
    for path in paths:
        path.write_bytes(b"unchanged content")
        path.chmod(0o640)
        os.utime(path, ns=(atime_ns, mtime_ns))
    (root / "link_20261027_203457.mp4").symlink_to(outside)
    (root / "linked directory").symlink_to(tmp_path, target_is_directory=True)
    directory_mtimes = {path: path.stat().st_mtime_ns for path in (root, nested)}

    def run(*args):
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "repair_output_mtimes.py"), str(root), *args],
            env={**os.environ, "TZ": "Europe/Warsaw"},
            capture_output=True, text=True, timeout=10,
        )

    dry_run = run("--dry-run")
    assert dry_run.returncode == 0, dry_run.stderr
    assert "Would update: 2" in dry_run.stdout
    assert all(path.stat().st_mtime_ns == mtime_ns for path in paths)

    applied = run()
    assert applied.returncode == 0, applied.stderr
    assert "2026-10-27T20:34:57+01:00" in applied.stdout
    assert "2026-08-01T12:00:00+02:00" in applied.stdout
    assert "Updated: 2; invalid dates skipped: 2; errors: 0" in applied.stdout
    assert winter.stat().st_mtime_ns == 1_793_129_697_000_000_000
    assert summer.stat().st_mtime_ns == 1_785_578_400_000_000_000
    for path in (invalid, clock_gap, unrelated, outside):
        assert path.stat().st_mtime_ns == mtime_ns
    assert all(path.stat().st_mtime_ns == value for path, value in directory_mtimes.items())
    for path in paths:
        assert path.stat().st_atime_ns == atime_ns
        assert path.stat().st_mode & 0o777 == 0o640
        assert path.read_bytes() == b"unchanged content"

    assert "Updated: 0" in run().stdout
