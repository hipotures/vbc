#!/usr/bin/env python3
"""Separate screen captures from renamed Android camera recordings."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class Entry:
    source: Path
    category: str
    destination: Path | None


def classify(tags: dict) -> str:
    """Match the two observed profiles; leave other profiles unclassified."""
    if tags.get("Error") or tags.get("Warning") or not tags.get("AndroidVersion"):
        return "unknown"
    dimensions = (tags.get("ImageWidth"), tags.get("ImageHeight"))
    codec = tags.get("CompressorID")
    gps = tags.get("GPSCoordinates")
    if dimensions == (3840, 2160) and codec == "hvc1" and gps:
        return "camera"
    if (
        dimensions == (1080, 2400)
        and codec == "avc1"
        and not gps
        and tags.get("Rotation") == 0
    ):
        return "screen"
    return "unknown"


def read_tags(paths: Sequence[Path]) -> dict[Path, dict]:
    """Read metadata in bounded batches before any files are moved."""
    tags_by_path = {}
    for start in range(0, len(paths), 200):
        batch = paths[start : start + 200]
        result = subprocess.run(
            [
                "exiftool", "-json", "-n", "-ImageWidth", "-ImageHeight",
                "-CompressorID", "-GPSCoordinates", "-AndroidVersion", "-Rotation",
                *(str(path) for path in batch),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise ValueError(f"ExifTool scan failed: {result.stderr.strip()}")
        rows = json.loads(result.stdout)
        if not isinstance(rows, list):
            raise ValueError("ExifTool returned an invalid metadata list")
        for row in rows:
            if not isinstance(row, dict) or not row.get("SourceFile"):
                raise ValueError("ExifTool returned an invalid metadata entry")
            tags_by_path[Path(row["SourceFile"])] = row
        if any(path not in tags_by_path for path in batch):
            raise ValueError("ExifTool omitted files from the metadata scan")
    return tags_by_path


def validate_destination(path: Path, output: Path) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"Destination already exists: {path}")
    for parent in path.parents:
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise ValueError(f"Invalid destination directory: {parent}")
        if parent == output:
            break


def build_plan(folders: Sequence[Path], output: Path) -> list[Entry]:
    roots = [folder.expanduser().resolve(strict=True) for folder in folders]
    output = output.expanduser().resolve()
    if any(not root.is_dir() for root in roots):
        raise ValueError("Each input must be a directory")
    if len({root.name for root in roots}) != len(roots):
        raise ValueError("Input folders must have different names")
    for index, root in enumerate(roots):
        if output.is_relative_to(root) or root.is_relative_to(output):
            raise ValueError("Output and input folders must not overlap")
        for other in roots[index + 1 :]:
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise ValueError("Input folders must not overlap")

    files: list[tuple[Path, Path]] = []
    for root in roots:
        def scan_error(error: OSError) -> None:
            raise error

        for directory, directories, names in os.walk(root, onerror=scan_error):
            directories[:] = sorted(
                name for name in directories
                if not (Path(directory) / name).is_symlink()
            )
            for name in sorted(names):
                path = Path(directory) / name
                if path.suffix.lower() == ".mp4" and not path.is_symlink() and path.is_file():
                    files.append((root, path))

    metadata = read_tags([path for _, path in files])
    plan = []
    for root, path in files:
        category = classify(metadata[path])
        destination = (
            output / root.name / path.relative_to(root)
            if category == "screen" else None
        )
        if destination is not None:
            validate_destination(destination, output)
        plan.append(Entry(path, category, destination))
    return plan


def move_file(source: Path, destination: Path) -> None:
    """Move without overwriting; use an exclusive copy across filesystems."""
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Source is no longer a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination, follow_symlinks=False)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        with destination.open("xb") as target:
            try:
                with source.open("rb") as original:
                    shutil.copyfileobj(original, target)
                target.flush()
                os.fsync(target.fileno())
                shutil.copystat(source, destination)
            except BaseException:
                destination.unlink()
                raise
    source.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="+", type=Path, help="Input folders to scan recursively")
    parser.add_argument("--output", required=True, type=Path, help="Destination for screen recordings")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without creating or moving anything")
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    try:
        plan = build_plan(args.folders, output)
        counts = Counter(entry.category for entry in plan)
        for entry in plan:
            if entry.destination is not None:
                action = "WOULD MOVE" if args.dry_run else "MOVE"
                print(f"{action}: {entry.source} -> {entry.destination}")
            else:
                print(f"KEEP [{entry.category}]: {entry.source}")
        print(
            f"Summary: camera={counts['camera']}, screen={counts['screen']}, "
            f"unknown={counts['unknown']}; mode={'dry-run' if args.dry_run else 'move'}",
            flush=True,
        )
        if not args.dry_run:
            for entry in plan:
                if entry.destination is not None:
                    validate_destination(entry.destination, output)
                    move_file(entry.source, entry.destination)
                    print(f"MOVED: {entry.source} -> {entry.destination}", flush=True)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
