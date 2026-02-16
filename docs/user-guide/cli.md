# CLI Reference

Complete reference for VBC command-line interface.

## Basic Syntax

```bash
uv run vbc [INPUT_DIR] [OPTIONS]
```

## Positional Arguments

### `INPUT_DIR`

Optional. Directory containing videos to compress.
If omitted, VBC uses `input_dirs` from the config file (CLI overrides config, no merge).

```bash
uv run vbc /path/to/videos
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
uv run vbc /videos --config custom.yaml
```

#### `--threads INT`, `-t INT`

Number of concurrent compression threads.

**Default:** From config (default: 1)

```bash
uv run vbc /videos --threads 8
```

**Note:** Runtime keyboard adjustment (`<`/`>`) clamps to 1-8. Startup value from CLI/config accepts `>0` (practical upper bound: executor `max_workers=16`).

#### `--quality INT`

Quality value (0-63). Lower = better quality.

**Default:** From encoder args (`gpu_encoder`/`cpu_encoder`, via `-cq` or `-crf`)
**Rule:** Cannot be used with `--quality-mode rate`.

```bash
uv run vbc /videos --quality 38
```

**Recommendations:**
- 35-38: Archival quality
- 40-45: High quality
- 48-52: Good quality
- 55+: Low quality

#### `--quality-mode TEXT`

Select quality control mode: `cq` (default) or `rate`.

**Default:** `cq`

```bash
# Default behavior (CQ/CRF from encoder args)
uv run vbc /videos --quality-mode cq --quality 38

# Bitrate target mode
uv run vbc /videos --quality-mode rate --bps 200Mbps
```

**Validation rules:**
- `--quality` cannot be used with `--quality-mode rate`
- `--bps` / `--minrate` / `--maxrate` require `--quality-mode rate`

#### `--bps TEXT`

Target video bitrate used when `--quality-mode rate`.

Accepted formats:
- Absolute: `200000000`, `200000k`, `200M`, `200Mbps`
- Relative: `0.8` (input bitrate × 0.8)

```bash
uv run vbc /videos --quality-mode rate --bps 200Mbps
uv run vbc /videos --quality-mode rate --bps 0.8
```

#### `--minrate TEXT`

Optional minimum bitrate clamp for `rate` mode.
Must be the same numeric class as `--bps` (all absolute or all relative).

```bash
uv run vbc /videos --quality-mode rate --bps 0.8 --minrate 0.7 --maxrate 0.9
```

#### `--maxrate TEXT`

Optional maximum bitrate clamp for `rate` mode.
Must be the same numeric class as `--bps` and `--minrate`.

### Encoder

#### `--gpu` / `--cpu`

Enable/disable GPU acceleration.

**Default:** From config (`general.gpu`, default `true`)

```bash
# Use GPU (NVENC AV1)
uv run vbc /videos --gpu

# Use CPU (SVT-AV1)
uv run vbc /videos --cpu
```

**Trade-offs:**
- `--gpu`: Fast, good for 1080p/1440p, quality ceiling ~CQ35-38
- `--cpu`: Slow, excellent quality at any resolution

### Audio

Audio handling is automatic:
- Lossless audio (`pcm_*`, `flac`, `alac`, `truehd`, `mlp`, `wavpack`, `ape`, `tta`) is transcoded to AAC at 256 kbps.
- AAC/MP3 are stream-copied.
- Other/unknown codecs are transcoded to AAC at 192 kbps for MP4 compatibility.
- Files without audio remain silent.

Lossless codec detection (ffprobe `codec_name`) in practice:
- FLAC → `flac`
- ALAC → `alac`
- TrueHD → `truehd`
- PCM → `pcm_*` (e.g. `pcm_s16be`, `pcm_s16le`, `pcm_s24le`)
- MLP → `mlp`
- WavPack → `wavpack`
- APE → `ape`
- TTA → `tta`

**Debug hint:** `--debug` logs show `AUDIO_MODE` with the detected codec and action.

### Queue

#### `--queue-sort TEXT`

Queue ordering mode.

**Default:** From config (`name`)

**Values:** `name`, `rand`, `dir`, `size`, `size-asc`, `size-desc`, `ext`
**Note:** `ext` uses the order defined in `extensions`.

```bash
# Sort by file size (small → large)
uv run vbc /videos --queue-sort size

# Process directories in CLI order, sort within each directory
uv run vbc /dir1,/dir2 --queue-sort dir
```

#### `--queue-seed INT`

Seed for deterministic random order when using `--queue-sort rand`.

```bash
uv run vbc /videos --queue-sort rand --queue-seed 42
```

### Filtering

#### `--skip-av1`

Skip files already encoded in AV1 codec.

**Default:** false

```bash
uv run vbc /videos --skip-av1
```

**Use case:** Mixed library with some AV1 files.

#### `--camera TEXT`

Only process files from specific camera models (comma-separated).

**Default:** All cameras

```bash
# Single camera
uv run vbc /videos --camera "ILCE-7RM5"

# Multiple cameras
uv run vbc /videos --camera "ILCE-7RM5,DJI,DC-GH7"
```

**Matching:** Substring, case-insensitive. Exact model strings are most reliable.

#### `--min-size INT`

Minimum input file size in bytes.

**Default:** 1048576 (1 MiB)

```bash
# 5 MiB minimum
uv run vbc /videos --min-size 5242880

# No minimum (process all files)
uv run vbc /videos --min-size 0
```

#### `--min-ratio FLOAT`

Minimum compression ratio required (0.0-1.0).

**Default:** 0.1 (10% savings)

```bash
# Require 20% savings
uv run vbc /videos --min-ratio 0.2

# Accept any compression
uv run vbc /videos --min-ratio 0.0
```

**Behavior:** If savings < threshold, original file copied to output (not compressed version).

### Rotation

#### `--rotate-180`

Rotate all videos 180 degrees.

**Default:** false

```bash
uv run vbc /videos --rotate-180
```

**Note:** Overrides auto-rotation patterns from config.

### Error Handling

#### `--clean-errors`

Remove existing `.err` markers on startup and retry those files.

**Default:** false (skip files with .err)

```bash
uv run vbc /videos --clean-errors
```

**Use case:** Fixed issue causing errors, want to retry.

### Logging

#### `--debug` / `--no-debug`

Enable/disable verbose debug logging.

**Default:** `--no-debug`

```bash
uv run vbc /videos --debug
```

**Debug logs:**
- FFmpeg timing (FFMPEG_START, FFMPEG_END)
- ExifTool calls (EXIF_COPY_START, EXIF_COPY_DONE)
- Compression stages (PROCESS_START, PROCESS_END)
- Metadata cache misses

#### `--log-path PATH`

Path to log file (overrides config).

```bash
uv run vbc /videos --log-path /tmp/vbc/compression.log
```

**Related diagnostics:**
- If `log_path` is `null`, logs are written to `<output_dir>/compression.log`.
- Uncaught fatal exceptions are appended to `error.log` in the current working directory.
- Per-file failures create `.err` markers (moved to errors dir after processing).

### Demo

#### `--demo`

Run simulated processing (no video file processing I/O). The UI and event flow behave like a real run, and logs are still written.

**Note:** `INPUT_DIR` is optional; in demo mode it is ignored.

```bash
uv run vbc --demo
```

#### `--demo-config PATH`

Path to demo simulation settings.

**Default:** `conf/demo.yaml`

```bash
uv run vbc --demo --demo-config conf/demo.yaml
```

## Examples

### Basic Compression

```bash
# Default settings (threads=1, quality from encoder args, GPU)
uv run vbc /videos
```

### High Quality Archive

```bash
# CPU mode, low CQ, 4 threads
uv run vbc /videos --cpu --quality 35 --threads 4
```

### Fast GPU Compression

```bash
# GPU mode, 8 threads, standard quality
uv run vbc /videos --gpu --threads 8 --quality 45
```

### Camera-Specific Processing

```bash
# Only ILCE-7RM5 cameras, high quality
uv run vbc /videos \
  --camera "ILCE-7RM5" \
  --quality 38 \
  --threads 6
```

### Retry Failed Jobs

```bash
# Remove error markers and retry
uv run vbc /videos --clean-errors
```

### Debug Run

```bash
# Debug logging, fewer threads for clarity
uv run vbc /videos --threads 2 --debug
```

### Complete Custom Run

```bash
uv run vbc /videos \
  --config conf/production.yaml \
  --threads 12 \
  --quality 40 \
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
  gpu: true

gpu_encoder:
  common_args:
    - "-cq 45"

cpu_encoder:
  common_args:
    - "-crf 32"
```

```bash
uv run vbc /videos --threads 8 --quality 38 --cpu
```

**Result:**
- `threads`: 8 (CLI override)
- `quality`: 38 (overrides `-cq`/`-crf` in encoder args)
- `gpu`: false (CLI override: `--cpu`)

## Environment Variables

VBC does not use environment variables. All configuration via YAML or CLI.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success (including graceful shutdown via `S`) |
| 1 | Error (invalid config, directory not found, fatal exception) |
| 130 | Interrupted (Ctrl+C) |

## Output Files

### Compressed Videos

```
{INPUT_DIR}_out/{relative_path}{ext}
```

By default, output files are `.mp4`. If encoder args include a format flag (e.g. `-f matroska` or `-f mov`), VBC uses the matching file extension (e.g. `.mkv` or `.mov`).
Default output directory uses `suffix_output_dirs` (default `_out`); you can override per-input with `output_dirs`.

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
{INPUT_DIR}_out/{relative_path}.err   # during run
{INPUT_DIR}_err/{relative_path}.err   # final location after run (default suffix_errors_dirs)
```

Created for failed compressions. Contains error message.
These markers are written under the output directory during processing and moved to
`errors_dirs`/`suffix_errors_dirs` after the run completes (if configured).

**Example:**
```
$ cat /videos_err/video.err
Hardware is lacking required capabilities
```

## Shell Completion

**Bash:**
```bash
# Add to ~/.bashrc
eval "$(uv run vbc --show-completion bash)"
```

**Zsh:**
```bash
# Add to ~/.zshrc
eval "$(uv run vbc --show-completion zsh)"
```

## Next Steps

- [Runtime Controls](runtime-controls.md) - Keyboard shortcuts
- [Advanced Features](advanced.md) - Dynamic Quality, auto-rotation
- [Configuration](../getting-started/configuration.md) - YAML settings
