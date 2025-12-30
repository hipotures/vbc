# VBC Utilities

Helper scripts for VBC compression workflow. These utilities work with VBC output directories (`*_out/`).

## move_err_files.py

Helper script that relocates source MP4 files for which the compressor created `.err` markers.

### Features

- Derives output directory by appending `_out` to the input directory path
- Finds all `.err` files in the output tree and moves their source `.mp4` counterparts to a safe location
- Preserves relative directory structure under the destination (default: `/tmp/err`)
- Moves `.err` files alongside their source videos after a successful move
- Prompts for confirmation when more than 20 `.err` files are detected; otherwise runs without prompts

### Requirements

- Python 3.9+

### Usage

```bash
# Move all errored videos and .err markers to /tmp/err
python vbc/utils/move_err_files.py /run/media/xai/.../QVR

# Custom destination
python vbc/utils/move_err_files.py /run/media/xai/.../QVR --dest /path/to/quarantine
```

### Output

- Moved `.mp4` and `.err` files appear under the destination, keeping their original subdirectory structure.
- Summary printed with counts of moved files and any missing sources.

---

## copy_failed_videos.py

Copy source videos for failed compressions based on `.err` files.

### Features

- Finds all `.err` files in the output directory
- Copies corresponding source `.mp4` files to a new directory
- Preserves relative directory structure from source
- Reports missing source files
- Prints summary with counts

### Requirements

- Python 3.9+

### Usage

```bash
# Copy failed videos to new directory
python vbc/utils/copy_failed_videos.py /path/to/SR /path/to/SR_out /path/to/SR_new
```

**Arguments:**
1. `source_dir` - Directory with original `.mp4` files (e.g., SR)
2. `error_dir` - Directory with `.err` files (e.g., SR_out)
3. `destination_dir` - Destination directory (e.g., SR_new)

### Output

- Copied `.mp4` files appear in destination directory with preserved structure
- Console summary showing:
  - Number of files copied
  - Number of source files not found
  - Total `.err` files processed
