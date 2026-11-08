#!/usr/bin/env python3
"""
Copy source videos for failed compressions based on .err files.

This script finds all .err files in the output directory and copies
the corresponding source .mp4 files to a new directory, preserving
the date subdirectory structure.
"""

import sys
import shutil
from pathlib import Path


def copy_failed_videos(source_dir: str, error_dir: str, destination_dir: str):
    """
    Find .err files and copy corresponding source videos.

    Args:
        source_dir: Directory with original .mp4 files (e.g., SR)
        error_dir: Directory with .err files (e.g., SR_out)
        destination_dir: Destination directory (e.g., SR_new)
    """
    source_path = Path(source_dir)
    error_path = Path(error_dir)
    dest_path = Path(destination_dir)

    if not source_path.exists():
        print(f"Error: Source directory does not exist: {source_dir}")
        sys.exit(1)

    if not error_path.exists():
        print(f"Error: Error directory does not exist: {error_dir}")
        sys.exit(1)

    # Find all .err files
    err_files = list(error_path.rglob("*.err"))

    if not err_files:
        print(f"No .err files found in {error_dir}")
        return

    print(f"Found {len(err_files)} .err files")
    print(f"Source directory: {source_dir}")
    print(f"Destination directory: {destination_dir}")
    print()

    copied = 0
    not_found = 0

    for err_file in err_files:
        # Get the base name without .err extension
        video_name = err_file.stem + ".mp4"

        # Get relative path from error_dir to maintain structure
        relative_path = err_file.parent.relative_to(error_path)

        # Construct source and destination paths
        source_video = source_path / relative_path / video_name
        dest_video = dest_path / relative_path / video_name

        if source_video.exists():
            # Create destination directory if needed
            dest_video.parent.mkdir(parents=True, exist_ok=True)

            # Copy the file
            print(f"Copying: {relative_path / video_name}")
            shutil.copy2(source_video, dest_video)
            copied += 1
        else:
            print(f"WARNING: Source file not found: {source_video}")
            not_found += 1

    print()
    print("Summary:")
    print(f"  Copied: {copied}")
    print(f"  Not found: {not_found}")
    print(f"  Total .err files: {len(err_files)}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: copy_failed_videos.py <source_dir> <error_dir> <destination_dir>")
        print()
        print("Example:")
        print("  copy_failed_videos.py /path/to/SR /path/to/SR_out /path/to/SR_new")
        sys.exit(1)

    source_dir = sys.argv[1]
    error_dir = sys.argv[2]
    dest_dir = sys.argv[3]

    copy_failed_videos(source_dir, error_dir, dest_dir)
