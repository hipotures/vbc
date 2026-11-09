#!/usr/bin/env python3
"""Restore file mtimes from local YYYYMMDD_HHMMSS timestamps in filenames."""

import argparse
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path

TIMESTAMP = re.compile(r"(?<!\d)(\d{8}_\d{6})(?!\d)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="preview changes without writing")
    args = parser.parse_args()
    if args.directory.is_symlink() or not args.directory.is_dir():
        parser.error("directory must be an existing directory, not a symlink")

    changed = invalid = errors = 0

    def report_error(exc: OSError) -> None:
        nonlocal errors
        errors += 1
        print(f"ERROR: {exc}", file=sys.stderr)

    for directory, _, names in os.walk(args.directory, followlinks=False, onerror=report_error):
        for name in sorted(names):
            match = TIMESTAMP.search(name)
            if match is None:
                continue
            path = Path(directory) / name
            try:
                before = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode):
                    continue
                try:
                    local_date = datetime.strptime(match[1], "%Y%m%d_%H%M%S")
                    timestamp_ns = int(local_date.timestamp()) * 1_000_000_000
                    if datetime.fromtimestamp(timestamp_ns / 1_000_000_000) != local_date:
                        raise ValueError("nonexistent local time during a clock change")
                except (ValueError, OverflowError) as exc:
                    invalid += 1
                    print(f"SKIP: {path}: {exc}", file=sys.stderr)
                    continue
                if before.st_mtime_ns == timestamp_ns:
                    continue
                if not args.dry_run:
                    os.utime(path, ns=(before.st_atime_ns, timestamp_ns), follow_symlinks=False)
                    if path.stat(follow_symlinks=False).st_mtime_ns != timestamp_ns:
                        raise OSError(f"modification time was not preserved: {path}")
                action = "WOULD SET" if args.dry_run else "SET"
                print(f"{action}: {path} -> {local_date.astimezone().isoformat()}")
                changed += 1
            except OSError as exc:
                report_error(exc)

    action = "Would update" if args.dry_run else "Updated"
    print(f"{action}: {changed}; invalid dates skipped: {invalid}; errors: {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
