# CLI Reference

Complete reference for VBC command-line interface.

## Basic Syntax

```bash
uv run vbc/main.py [INPUT_DIR] [OPTIONS]
```

## Positional Arguments

### `INPUT_DIR`

Optional. Directory containing videos to compress.
If omitted, VBC uses `input_dirs` from the config file (CLI overrides config, no merge).

```bash
uv run vbc/main.py /path/to/videos
```

**Behavior:**
- Accepts a single directory or a comma-separated list
- Output directory: `{INPUT_DIR}_out` (per input dir)
- Skips subdirectories ending in `_out`
- Limits: max 50 directories, max 150 characters per entry
- Duplicates ignored (first occurrence wins), missing/inaccessible directories skipped

## Options

### General

#### `--config PATH`, `-c PATH`

Path to YAML configuration file.

**Default:** `conf/vbc.yaml`

```bash
uv run vbc/main.py /videos --config custom.yaml
```

#### `--threads INT`, `-t INT`

Number of concurrent compression threads (1-16).

**Default:** From config (usually 4)

```bash
uv run vbc/main.py /videos --threads 8
```

**Note:** Can be adjusted at runtime with `<`/`>` keys.

#### `--cq INT`

Constant quality value (0-63). Lower = better quality.

**Default:** From config (usually 45)

```bash
uv run vbc/main.py /videos --cq 38
```

**Recommendations:**
- 35-38: Archival quality
- 40-45: High quality
- 48-52: Good quality
- 55+: Low quality

### Encoder

#### `--gpu` / `--cpu`

Enable/disable GPU acceleration.

**Default:** `--gpu` (NVENC)

```bash
# Use GPU (NVENC AV1)
uv run vbc/main.py /videos --gpu

# Use CPU (SVT-AV1)
uv run vbc/main.py /videos --cpu
```

**Trade-offs:**
- `--gpu`: Fast, good for 1080p/1440p, quality ceiling ~CQ35-38
- `--cpu`: Slow, excellent quality at any resolution

### Queue

#### `--queue-sort TEXT`

Queue ordering mode.

**Default:** From config (`name`)

**Values:** `name`, `rand`, `dir`, `size`, `size-asc`, `size-desc`, `ext`
**Note:** `ext` uses the order defined in `extensions`.

```bash
# Sort by file size (small → large)
uv run vbc/main.py /videos --queue-sort size

# Process directories in CLI order, sort within each directory
uv run vbc/main.py /dir1,/dir2 --queue-sort dir
```

#### `--queue-seed INT`

Seed for deterministic random order when using `--queue-sort rand`.

```bash
uv run vbc/main.py /videos --queue-sort rand --queue-seed 42
```

### Filtering

#### `--skip-av1`

Skip files already encoded in AV1 codec.

**Default:** false

```bash
uv run vbc/main.py /videos --skip-av1
```

**Use case:** Mixed library with some AV1 files.

#### `--camera TEXT`

Only process files from specific camera models (comma-separated).

**Default:** All cameras

```bash
# Single camera
uv run vbc/main.py /videos --camera "Sony"

# Multiple cameras
uv run vbc/main.py /videos --camera "Sony,DJI,ILCE-7RM5"
```

**Matching:** Substring, case-insensitive.

#### `--min-size INT`

Minimum input file size in bytes.

**Default:** 1048576 (1 MiB)

```bash
# 5 MiB minimum
uv run vbc/main.py /videos --min-size 5242880

# No minimum (process all files)
uv run vbc/main.py /videos --min-size 0
```

#### `--min-ratio FLOAT`

Minimum compression ratio required (0.0-1.0).

**Default:** 0.1 (10% savings)

```bash
# Require 20% savings
uv run vbc/main.py /videos --min-ratio 0.2

# Accept any compression
uv run vbc/main.py /videos --min-ratio 0.0
```

**Behavior:** If savings < threshold, original file copied to output (not compressed version).

### Rotation

#### `--rotate-180`

Rotate all videos 180 degrees.

**Default:** false

```bash
uv run vbc/main.py /videos --rotate-180
```

**Note:** Overrides auto-rotation patterns from config.

### Error Handling

#### `--clean-errors`

Remove existing `.err` markers on startup and retry those files.

**Default:** false (skip files with .err)

```bash
uv run vbc/main.py /videos --clean-errors
```

**Use case:** Fixed issue causing errors, want to retry.

### Logging

#### `--debug` / `--no-debug`

Enable/disable verbose debug logging.

**Default:** `--no-debug`

```bash
uv run vbc/main.py /videos --debug
```

**Debug logs:**
- FFmpeg timing (FFMPEG_START, FFMPEG_END)
- ExifTool calls (EXIF_COPY_START, EXIF_COPY_DONE)
- Compression stages (PROCESS_START, PROCESS_END)
- Metadata cache misses

#### `--log-path PATH`

Path to log file (overrides config).

```bash
uv run vbc/main.py /videos --log-path /tmp/vbc/compression.log
```

### Demo

#### `--demo`

Run simulated processing (no file IO). The UI and event flow behave like a real run.

**Note:** `INPUT_DIR` is optional; in demo mode it is ignored.

```bash
uv run vbc/main.py --demo
```

#### `--demo-config PATH`

Path to demo simulation settings.

**Default:** `conf/demo.yaml`

```bash
uv run vbc/main.py --demo --demo-config conf/demo.yaml
```

## Examples

### Basic Compression

```bash
# Default settings (4 threads, CQ=45, GPU)
uv run vbc/main.py /videos
```

### High Quality Archive

```bash
# CPU mode, low CQ, 4 threads
uv run vbc/main.py /videos --cpu --cq 35 --threads 4
```

### Fast GPU Compression

```bash
# GPU mode, 8 threads, standard quality
uv run vbc/main.py /videos --gpu --threads 8 --cq 45
```

### Camera-Specific Processing

```bash
# Only Sony cameras, high quality
uv run vbc/main.py /videos \
  --camera "Sony" \
  --cq 38 \
  --threads 6
```

### Retry Failed Jobs

```bash
# Remove error markers and retry
uv run vbc/main.py /videos --clean-errors
```

### Debug Run

```bash
# Debug logging, fewer threads for clarity
uv run vbc/main.py /videos --threads 2 --debug
```

### Complete Custom Run

```bash
uv run vbc/main.py /videos \
  --config conf/production.yaml \
  --threads 12 \
  --cq 40 \
  --gpu \
  --camera "ILCE-7RM5,DC-GH7" \
  --skip-av1 \
  --min-size 10485760 \
  --min-ratio 0.15 \
  --clean-errors \
  --debug
```

## Configuration Priority

CLI arguments **override** config file settings.

**Example:**

`conf/vbc.yaml`:
```yaml
general:
  threads: 4
  cq: 45
  gpu: true
```

```bash
uv run vbc/main.py /videos --threads 8 --cq 38 --cpu
```

**Result:**
- `threads`: 8 (CLI override)
- `cq`: 38 (CLI override)
- `gpu`: false (CLI override: `--cpu`)

## Environment Variables

VBC does not use environment variables. All configuration via YAML or CLI.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success (all files processed) |
| 1 | Error (invalid config, directory not found, fatal exception) |
| 130 | Interrupted (Ctrl+C or graceful shutdown) |

## Output Files

### Compressed Videos

```
{INPUT_DIR}_out/{relative_path}.mp4
```

All output files are `.mp4` regardless of input extension.

**Example:**
```
Input:  /videos/subfolder/video.avi
Output: /videos_out/subfolder/video.mp4
```

### Log File

```
/tmp/vbc/compression.log
```

Detailed log of all operations (INFO and ERROR levels). Override with `--log-path` or `general.log_path`.

### Error Markers

```
{INPUT_DIR}_out/{relative_path}.err
```

Created for failed compressions. Contains error message.

**Example:**
```
$ cat /videos_out/video.err
Hardware is lacking required capabilities
```

## Shell Completion

**Bash:**
```bash
# Add to ~/.bashrc
eval "$(uv run vbc/main.py --show-completion bash)"
```

**Zsh:**
```bash
# Add to ~/.zshrc
eval "$(uv run vbc/main.py --show-completion zsh)"
```

## Next Steps

- [Runtime Controls](runtime-controls.md) - Keyboard shortcuts
- [Advanced Features](advanced.md) - Dynamic CQ, auto-rotation
- [Configuration](../getting-started/configuration.md) - YAML settings
