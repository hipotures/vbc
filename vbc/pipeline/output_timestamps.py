"""Filesystem timestamp handling for completed compression outputs."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TimestampUpdate:
    """Filesystem entries whose modification time changed."""

    file_paths: tuple[Path, ...] = ()
    directory_paths: tuple[Path, ...] = ()

    @property
    def files(self) -> int:
        return len(self.file_paths)

    @property
    def directories(self) -> int:
        return len(self.directory_paths)


def apply_output_timestamps(
    output_paths: Iterable[Path],
    source_mtime_ns: int,
) -> TimestampUpdate:
    """Set output mtimes exactly and only advance their immediate directories."""
    if source_mtime_ns < 0:
        raise ValueError("source mtime cannot be negative")

    paths = tuple(dict.fromkeys(Path(path) for path in output_paths))
    if not paths:
        return TimestampUpdate()

    files_updated: list[Path] = []
    parents: set[Path] = set()
    for output_path in paths:
        if output_path.is_symlink() or not output_path.is_file():
            raise OSError(f"output is not a regular file: {output_path}")
        output_stat = output_path.stat()
        if output_stat.st_mtime_ns != source_mtime_ns:
            os.utime(
                output_path,
                ns=(output_stat.st_atime_ns, source_mtime_ns),
                follow_symlinks=False,
            )
            files_updated.append(output_path)
        parents.add(output_path.parent)

    directories_updated: list[Path] = []
    for parent in parents:
        if parent.is_symlink() or not parent.is_dir():
            raise OSError(f"output parent is not a regular directory: {parent}")
        parent_stat = parent.stat()
        if parent_stat.st_mtime_ns < source_mtime_ns:
            os.utime(
                parent,
                ns=(parent_stat.st_atime_ns, source_mtime_ns),
                follow_symlinks=False,
            )
            directories_updated.append(parent)

    return TimestampUpdate(
        file_paths=tuple(files_updated),
        directory_paths=tuple(directories_updated),
    )
