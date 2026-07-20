#!/usr/bin/env python3
"""Restore file and directory mtimes from timestamps embedded in filenames."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.table import Table

_FILENAME_TIMESTAMP = re.compile(r"(?<!\d)(\d{8}_\d{6})(?!\d)")
_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"


class OutputMtimeRepairError(RuntimeError):
    """Raised when the repair target cannot be inspected safely."""


@dataclass
class RepairResult:
    files_scanned: int = 0
    matching_files: int = 0
    files_updated: int = 0
    directories_considered: int = 0
    directories_updated: int = 0
    invalid_dates: int = 0
    symlinks_ignored: int = 0
    dry_run: bool = False
    issues: list[str] = field(default_factory=list)


def _timestamp_from_name(name: str) -> tuple[int | None, bool]:
    """Return local-time epoch nanoseconds and whether a date-like value existed."""
    matches = _FILENAME_TIMESTAMP.findall(name)
    for value in matches:
        try:
            local_datetime = datetime.strptime(value, _TIMESTAMP_FORMAT).astimezone()
        except ValueError:
            continue
        return int(local_datetime.timestamp()) * 1_000_000_000, True
    return None, bool(matches)


def _would_change(path: Path, timestamp_ns: int) -> bool:
    return path.stat(follow_symlinks=False).st_mtime_ns != timestamp_ns


def _set_mtime(path: Path, timestamp_ns: int) -> bool:
    entry_stat = path.stat(follow_symlinks=False)
    if entry_stat.st_mtime_ns == timestamp_ns:
        return False
    os.utime(
        path,
        ns=(entry_stat.st_atime_ns, timestamp_ns),
        follow_symlinks=False,
    )
    return True


def repair_output_mtimes(
    root: Path,
    *,
    dry_run: bool = False,
    files_only: bool = False,
    user_dirs_only: bool = False,
) -> RepairResult:
    """Repair matching files recursively and their direct parent directories."""
    if files_only and user_dirs_only:
        raise ValueError("files_only and user_dirs_only are mutually exclusive")
    if root.is_symlink():
        raise OutputMtimeRepairError(f"target directory cannot be a symlink: {root}")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise OutputMtimeRepairError(f"not a directory: {root}")

    result = RepairResult(dry_run=dry_run)
    directory_timestamps: dict[Path, int] = {}

    for current_dir, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current_dir)
        retained_directories: list[str] = []
        for name in directory_names:
            directory_path = current_path / name
            if directory_path.is_symlink():
                result.symlinks_ignored += 1
            else:
                retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in file_names:
            path = current_path / name
            result.files_scanned += 1
            if path.is_symlink():
                result.symlinks_ignored += 1
                continue

            timestamp_ns, date_like = _timestamp_from_name(name)
            if timestamp_ns is None:
                if date_like:
                    result.invalid_dates += 1
                continue

            try:
                if not path.is_file():
                    result.issues.append(f"not a regular file, ignored: {path}")
                    continue
                result.matching_files += 1
                should_update_directory = not files_only and (
                    not user_dirs_only or current_path.parent == root
                )
                if should_update_directory:
                    previous = directory_timestamps.get(current_path)
                    if previous is None or timestamp_ns > previous:
                        directory_timestamps[current_path] = timestamp_ns
                if not user_dirs_only:
                    changed = (
                        _would_change(path, timestamp_ns)
                        if dry_run
                        else _set_mtime(path, timestamp_ns)
                    )
                    result.files_updated += int(changed)
            except OSError as exc:
                result.issues.append(f"cannot update file {path}: {exc}")

    result.directories_considered = len(directory_timestamps)
    for directory, timestamp_ns in directory_timestamps.items():
        try:
            changed = (
                _would_change(directory, timestamp_ns)
                if dry_run
                else _set_mtime(directory, timestamp_ns)
            )
            result.directories_updated += int(changed)
        except OSError as exc:
            result.issues.append(f"cannot update directory {directory}: {exc}")

    return result


def _render_result(console: Console, result: RepairResult) -> None:
    title = "Timestamp dry run" if result.dry_run else "Timestamps repaired"
    table = Table(title=title, show_header=False, box=None)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right", style="bold")
    table.add_row("Files scanned", str(result.files_scanned))
    table.add_row("Matching files", str(result.matching_files))
    table.add_row(
        "Files to update" if result.dry_run else "Files updated",
        str(result.files_updated),
    )
    table.add_row("Directories considered", str(result.directories_considered))
    table.add_row(
        "Directories to update" if result.dry_run else "Directories updated",
        str(result.directories_updated),
    )
    table.add_row("Invalid dates", str(result.invalid_dates))
    table.add_row("Symlinks ignored", str(result.symlinks_ignored))
    console.print(table)

    if result.issues:
        issues = Table(title="Issues", box=None)
        issues.add_column("#", justify="right", style="yellow")
        issues.add_column("Detail", style="red")
        for index, issue in enumerate(result.issues[:50], start=1):
            issues.add_row(str(index), issue)
        if len(result.issues) > 50:
            issues.add_row("…", f"{len(result.issues) - 50} more")
        console.print(issues)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Restore file mtimes from YYYYMMDD_HHMMSS in filenames and set each "
            "direct parent directory to its newest matching file timestamp."
        )
    )
    parser.add_argument("directory", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--files-only",
        action="store_true",
        help="update matching files without modifying directories",
    )
    modes.add_argument(
        "--user-dirs-only",
        action="store_true",
        help="update only direct child directories, without modifying files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show changes without modifying filesystem timestamps",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()
    try:
        with console.status("[cyan]Scanning filenames and timestamps…"):
            result = repair_output_mtimes(
                args.directory,
                dry_run=args.dry_run,
                files_only=args.files_only,
                user_dirs_only=args.user_dirs_only,
            )
    except (OSError, OutputMtimeRepairError) as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        return 1
    _render_result(console, result)
    return 1 if result.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
