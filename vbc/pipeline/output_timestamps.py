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
        # NFS: data still cached on the client reaches the server later and the
        # server then sets mtime to "now", wiping the time set here. Flush first,
        # set, flush again, and check that the time stuck.
        _flush(output_path)
        for _attempt in range(3):
            output_stat = output_path.stat()
            if output_stat.st_mtime_ns == source_mtime_ns:
                break
            os.utime(
                output_path,
                ns=(output_stat.st_atime_ns, source_mtime_ns),
                follow_symlinks=False,
            )
            _flush(output_path)
            if output_path not in files_updated:
                files_updated.append(output_path)
        else:
            if output_path.stat().st_mtime_ns != source_mtime_ns:
                raise OSError(f"output mtime does not stay set (filesystem rewrites it): {output_path}")

    return TimestampUpdate(
        file_paths=tuple(files_updated),
    )


def _flush(path: Path) -> None:
    """Write the file's cached data out to its storage (server, for NFS)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
