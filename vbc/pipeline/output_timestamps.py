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

    @property
    def files(self) -> int:
        return len(self.file_paths)


def apply_output_timestamps(
    output_paths: Iterable[Path],
    source_mtime_ns: int,
) -> TimestampUpdate:
    """Set completed output file mtimes exactly."""
    if source_mtime_ns < 0:
        raise ValueError("source mtime cannot be negative")

    paths = tuple(dict.fromkeys(Path(path) for path in output_paths))
    if not paths:
        return TimestampUpdate()

    files_updated: list[Path] = []
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

    return TimestampUpdate(
        file_paths=tuple(files_updated),
    )
