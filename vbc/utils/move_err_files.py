#!/usr/bin/env python3
"""
Move source MP4 files that have corresponding .err files in the output directory to /tmp/err.

Input directory is provided by the user; output directory is derived by appending `_out` to the
input directory name. Relative structure is preserved under /tmp/err. If more than 20 .err files
are found, the script asks for confirmation before moving anything.
"""

import argparse
import shutil
import sys
from pathlib import Path
from typing import Tuple


def confirm_large_batch(count: int, dest: Path) -> bool:
    prompt = f"Found {count} .err files. Move associated MP4s and .err files to {dest}? [y/N]: "
    answer = input(prompt).strip().lower()
    return answer.startswith("y")


def safe_move(src: Path, dest: Path) -> Tuple[bool, str]:
    """Move src to dest, creating parents; avoid overwriting existing dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return False, f"destination exists, skipping: {dest}"
    try:
        shutil.move(str(src), str(dest))
        return True, ""
    except Exception as e:
        return False, f"failed to move {src} -> {dest}: {e}"


def main():
    parser = argparse.ArgumentParser(description="Move MP4s with .err markers to /tmp/err")
    parser.add_argument("input_dir", type=Path, help="Input directory containing source .mp4 files")
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path("/tmp/err"),
        help="Destination directory for errored files (default: /tmp/err)",
    )
    args = parser.parse_args()

    input_dir = args.input_dir.resolve()
    if not input_dir.is_dir():
        print(f"Input directory does not exist or is not a directory: {input_dir}")
        sys.exit(1)

    output_dir = input_dir.with_name(f"{input_dir.name}_out")
    if not output_dir.is_dir():
        print(f"Output directory not found: {output_dir}")
        sys.exit(1)

    dest_dir = args.dest.resolve()
    err_files = sorted(output_dir.rglob("*.err"))

    if not err_files:
        print("No .err files found; nothing to move.")
        return

    if len(err_files) > 20 and not confirm_large_batch(len(err_files), dest_dir):
        print("Aborted by user.")
        return

    moved_mp4 = 0
    moved_err = 0
    missing_mp4 = 0
    skipped = 0

    for err_file in err_files:
        rel = err_file.relative_to(output_dir)
        src_mp4 = input_dir / rel.with_suffix(".mp4")
        dest_mp4 = dest_dir / rel.with_suffix(".mp4")
        dest_err = dest_dir / rel

        if src_mp4.exists():
            ok, msg = safe_move(src_mp4, dest_mp4)
            if ok:
                moved_mp4 += 1
            else:
                skipped += 1
                print(f"[WARN] {msg}")
        else:
            missing_mp4 += 1
            print(f"[WARN] Missing source MP4 for {rel}")

        ok, msg = safe_move(err_file, dest_err)
        if ok:
            moved_err += 1
        else:
            skipped += 1
            print(f"[WARN] {msg}")

    print(f"Moved MP4s: {moved_mp4}, moved .err: {moved_err}, missing MP4s: {missing_mp4}, skipped: {skipped}")


if __name__ == "__main__":
    main()
