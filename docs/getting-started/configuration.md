# Configuration

VBC can be configured via YAML files and CLI arguments. CLI arguments always override config file settings.

## Configuration File

Default location: `conf/vbc.yaml`

## Demo Configuration

Demo mode uses a separate simulation file: `conf/demo.yaml`.

```bash
uv run vbc --demo --demo-config conf/demo.yaml
```

This file controls simulated file counts, size distribution, processing speed, and error mix.

### Full Example

```yaml
input_dirs:
  - /path/to/videos
  - /path/to/folder with spaces

# When using output_dirs, set suffix_output_dirs to null.
output_dirs:
  - /path/to/videos_out
  - /path/to/folder with spaces_out

suffix_output_dirs: null

# When using errors_dirs, set suffix_errors_dirs to null.
errors_dirs:
  - /path/to/videos_err
  - /path/to/folder with spaces_err

suffix_errors_dirs: null

general:
  # === Core Settings ===
  threads: 8                    # Max concurrent compression threads (1-16)
  cq: 45                        # Default constant quality (0-63, lower=better)
  prefetch_factor: 1            # Submit-on-demand multiplier (1-5)
  gpu: true                     # Use GPU (NVENC) vs CPU (SVT-AV1)
  queue_sort: name              # Queue order: name, rand, dir, size, size-asc, size-desc, ext
  queue_seed: null              # Optional seed for deterministic rand order
  log_path: /tmp/vbc/compression.log  # Log file location
  cpu_fallback: false           # Retry on CPU if NVENC hits HW cap error
  ffmpeg_cpu_threads: null      # Max CPU threads per ffmpeg worker (null = auto)

  # === Input/Output ===
  extensions:                   # File extensions to process
    - ".mp4"
    - ".mov"
    - ".avi"
    - ".flv"
    - ".webm"
  min_size_bytes: 1048576       # Minimum file size (1 MiB)

  # === Metadata ===
  copy_metadata: true           # Copy EXIF/XMP tags from source
  use_exif: true                # Use ExifTool for deep metadata analysis

  # === Filtering ===
  skip_av1: false               # Skip files already encoded in AV1
  filter_cameras: []            # Only process specific camera models (empty = all)
                                # Example: ["Sony", "DJI OsmoPocket3"]

  # === Quality Control ===
  dynamic_cq:                   # Camera-specific CQ values
    "ILCE-7RM5": 38            # Sony A7R V - higher quality
    "DC-GH7": 40               # Panasonic GH7
    "DJI OsmoPocket3": 45      # DJI Pocket 3 - lower quality

  min_compression_ratio: 0.1    # Minimum savings required (0.0-1.0)
                                # If compression < 10%, keep original

  # === Error Handling ===
  clean_errors: false           # Remove .err markers on startup

  # === UI/Display ===
  strip_unicode_display: true   # Replace emoji/unicode with '?' in UI
  debug: false                  # Enable verbose debug logging

  # === Manual Rotation ===
  manual_rotation: null         # Global rotation (null, 0, 90, 180, 270)

gpu_config:
  enabled: true
  sample_interval_s: 5.0
  history_window_s: 300.0
  nvtop_device_index: 0

ui:
  activity_feed_max_items: 5
  panel_height_scale: 0.7

autorotate:
  patterns:                     # Regex -> Rotation angle
    "DJI_.*\\.MP4": 0          # DJI drones - no rotation
    "GOPR\\d+\\.MP4": 180      # GoPro pattern - 180° flip
    "IMG_\\d{4}\\.MOV": 90     # iPhone pattern - 90° rotation
```

## Configuration Reference

### General Settings

#### `threads`
- **Type**: Integer (>0)
- **Default**: 1
- **Description**: Maximum number of concurrent compression threads
- **Note**: Can be adjusted at runtime with `<` and `>` keys

#### `cq`
- **Type**: Integer (0-63)
- **Default**: 45
- **Description**: Default constant quality value. Lower = better quality, larger files.
- **Recommendations**:
  - 35-38: Archival quality
  - 40-45: High quality daily use
  - 48-52: Good quality, smaller files
  - 55+: Low quality, very small files

#### `prefetch_factor`
- **Type**: Integer (1-5)
- **Default**: 1
- **Description**: Submit-on-demand queue multiplier. Higher values = more files queued.
- **Formula**: `max_queued = prefetch_factor × threads`

#### `queue_sort`
- **Type**: String
- **Default**: `name`
- **Description**: Processing order for files in the queue
- **Values**: `name`, `rand`, `dir`, `size`, `size-asc`, `size-desc`, `ext`
- **Notes**:
  - `size` is an alias for `size-asc`
  - `rand` can be made deterministic with `queue_seed`
  - `ext` uses the order of `extensions` and requires a non-empty list

#### `queue_seed`
- **Type**: Integer or null
- **Default**: `null`
- **Description**: Seed for deterministic `rand` queue order
- **Example**: `42` (ensure same random order across runs)

#### `log_path`
- **Type**: String or null
- **Default**: `/tmp/vbc/compression.log`
- **Description**: Path to log file (overrides the output directory default)

#### `cpu_fallback`
- **Type**: Boolean
- **Default**: false
- **Description**: Retry on CPU when GPU encoding fails with hardware capability errors
- **Note**: Useful when NVENC runs out of sessions; pair with `ffmpeg_cpu_threads`
- **Behavior**: HW cap `.err` markers are cleared on startup so files re-enter the queue

#### `ffmpeg_cpu_threads`
- **Type**: Integer or null
- **Default**: `null`
- **Description**: Max CPU threads per ffmpeg worker when using CPU encoding (including fallback)
- **Note**: Limits per-worker CPU usage; does not change `threads` (worker count)

#### `gpu`
- **Type**: Boolean
- **Default**: true
- **Description**: Use GPU (NVENC) instead of CPU (SVT-AV1)
- **GPU (NVENC)**:
  - Pros: Very fast, good for 1080p/1440p
  - Cons: Quality ceiling at ~CQ35-38, session limits
- **CPU (SVT-AV1)**:
  - Pros: Excellent quality, no session limits
  - Cons: Much slower

#### `gpu_refresh_rate`
- **Type**: Integer
- **Default**: `5`
- **Description**: **(Deprecated)** Use `gpu_config.sample_interval_s` instead. Kept for backwards compatibility.
- **Note**: See deprecation notice in [GPU Monitoring](#gpu-monitoring-gpu_config) section

### GPU Monitoring (`gpu_config`)

Advanced settings for GPU monitoring sparklines.

#### `enabled`
- **Type**: Boolean
- **Default**: `true`
- **Description**: Enable GPU monitoring and dashboard sparklines.
- **Note**: Requires NVIDIA GPU and `nvidia-smi`.

#### `sample_interval_s`
- **Type**: Float
- **Default**: `5.0`
- **Description**: How often to sample GPU metrics (seconds).

#### `history_window_s`
- **Type**: Float
- **Default**: `300.0`
- **Description**: Total time window shown in sparklines (default 5 minutes).

#### `nvtop_device_index`
- **Type**: Integer
- **Default**: `0`
- **Description**: Index of the GPU to monitor when multiple GPUs are present.

#### `nvtop_device_name`
- **Type**: String or null
- **Default**: `null`
- **Description**: Override device selection by name instead of index (e.g., "NVIDIA GeForce RTX 4090").
- **Note**: When set, takes precedence over `nvtop_device_index`.

#### `refresh_rate`
- **Type**: Integer
- **Default**: `5`
- **Description**: **(Deprecated)** Use `sample_interval_s` instead. Kept for backwards compatibility.

!!! note "Deprecated Fields"
    - `gpu_config.refresh_rate` is **deprecated** in favor of `sample_interval_s`
    - `general.gpu_refresh_rate` is **deprecated** in favor of `gpu_config.sample_interval_s`

    For backwards compatibility, VBC still accepts both old fields, but new configurations should use `gpu_config.sample_interval_s`.

### UI Configuration (`ui`)

Dashboard display settings.

#### `activity_feed_max_items`
- **Type**: Integer (1-20)
- **Default**: `5`
- **Description**: Maximum number of events shown in the activity feed panel.

#### `active_jobs_max_display`
- **Type**: Integer (1-16)
- **Default**: `8`
- **Description**: Maximum number of concurrent jobs to display in the active panel.

#### `panel_height_scale`
- **Type**: Float (0.3-1.0)
- **Default**: `0.7`
- **Description**: Vertical scaling factor for panels (0.7 = 30% reduction in height).

### Input/Output

#### `input_dirs`
- **Type**: List of strings
- **Default**: `[]` (empty)
- **Description**: Default input directories when no CLI input is provided
- **Behavior**:
  - CLI input overrides config input (no merge)
  - Duplicates ignored (first occurrence wins)
  - Missing or inaccessible directories are skipped
  - Startup fails if no valid directories remain
  - Limits: max 50 directories, max 150 characters per entry

#### `output_dirs`
- **Type**: List of strings
- **Default**: `[]` (empty)
- **Description**: Explicit output directories (one per input directory, in order)
- **Rules**:
  - Must exist and be writable
  - Count must match input directories
  - Cannot be used with `suffix_output_dirs` (set it to `null`)

#### `suffix_output_dirs`
- **Type**: String or null
- **Default**: `_out`
- **Description**: Output directory suffix appended to each input directory name
- **Notes**:
  - Set to `null` when using `output_dirs`
  - Example: `/videos` → `/videos_out`

#### `errors_dirs`
- **Type**: List of strings
- **Default**: `[]` (empty)
- **Description**: Explicit directories for failed files (one per input directory, in order)
- **Rules**:
  - Must exist and be writable
  - Count must match input directories
  - Cannot be used with `suffix_errors_dirs` (set it to `null`)

#### `suffix_errors_dirs`
- **Type**: String or null
- **Default**: `_err`
- **Description**: Suffix appended to each input directory name for failed files
- **Behavior**: After processing, failed source files and their `.err` markers are moved here
- **Safety**: If more than 100 `.err` files are found, VBC asks before moving them
- **Notes**:
  - Set to `null` when using `errors_dirs`
  - Example: `/videos` → `/videos_err`

#### `extensions`
- **Type**: List of strings
- **Default**: `[".mp4", ".mov", ".avi", ".flv", ".webm"]`
- **Description**: File extensions to scan and process
- **Note**: Case-insensitive, can include or omit leading dot

#### `min_size_bytes`
- **Type**: Integer
- **Default**: 1048576 (1 MiB)
- **Description**: Minimum input file size to process
- **Use case**: Skip corrupted/incomplete files

### Metadata

#### `copy_metadata`
- **Type**: Boolean
- **Default**: true
- **Description**: Copy EXIF/XMP/GPS tags from source to output
- **Method**: Uses ExifTool to preserve all metadata including GPS

#### `use_exif`
- **Type**: Boolean
- **Default**: true
- **Description**: Enable deep metadata analysis with ExifTool
- **Required for**:
  - `dynamic_cq` (camera-specific quality)
  - `filter_cameras` (camera filtering)
  - GPS and camera model extraction

### Filtering

#### `skip_av1`
- **Type**: Boolean
- **Default**: false
- **Description**: Skip files already encoded in AV1 codec
- **Use case**: Mixed libraries with some AV1 files already compressed

#### `filter_cameras`
- **Type**: List of strings
- **Default**: `[]` (empty = process all cameras)
- **Description**: Only process files from specific camera models
- **Example**: `["Sony", "DJI", "ILCE-7RM5"]`
- **Matching**: Substring match (case-insensitive)

### Quality Control

#### `dynamic_cq`
- **Type**: Dictionary (string -> integer)
- **Default**: `{}` (empty)
- **Description**: Camera model -> CQ value mapping
- **Matching**: Full-text search in all EXIF metadata
- **Example**:
  ```yaml
  dynamic_cq:
    "ILCE-7RM5": 38      # Exact model match
    "Sony": 40           # Brand match (all Sony cameras)
    "DJI OsmoPocket3": 45
  ```
- **Priority**: First match wins (order matters in YAML)

#### `min_compression_ratio`
- **Type**: Float (0.0-1.0)
- **Default**: 0.1 (10%)
- **Description**: Minimum compression savings required
- **Behavior**: If `(1 - output_size/input_size) < threshold`, keep original file instead of compressed version
- **Use case**: Prevent "compression" that makes files larger

### Error Handling

#### `clean_errors`
- **Type**: Boolean
- **Default**: false
- **Description**: Remove existing `.err` markers on startup and retry those files
- **Behavior**:
  - `false`: Skip files with `.err` markers
  - `true`: Delete `.err` files and retry compression

### UI/Display

#### `strip_unicode_display`
- **Type**: Boolean
- **Default**: true
- **Description**: Replace non-ASCII characters (emoji, special Unicode) with '?' in UI
- **Reason**: Prevents table alignment issues with emoji in filenames

#### `debug`
- **Type**: Boolean
- **Default**: false
- **Description**: Enable verbose debug logging
- **Logs**: FFmpeg timing, ExifTool calls, compression stages

#### `manual_rotation`
- **Type**: Integer or null
- **Default**: null
- **Values**: null, 0, 90, 180, 270
- **Description**: Global rotation override (takes precedence over `autorotate`)

### Auto-Rotation

#### `patterns`
- **Type**: Dictionary (regex -> angle)
- **Default**: `{}`
- **Description**: Filename regex patterns mapped to rotation angles
- **Example**:
  ```yaml
  autorotate:
    patterns:
      "DJI_.*\\.MP4": 0        # No rotation for DJI drones
      "GOPR\\d+\\.MP4": 180    # 180° for GoPro pattern
      "IMG_\\d{4}\\.MOV": 90   # 90° for iPhone videos
  ```
- **Note**: First match wins (order matters)

## CLI Overrides

All config settings can be overridden via CLI:

```bash
# Override threads and CQ
uv run vbc /videos --threads 16 --cq 38

# Override GPU setting
uv run vbc /videos --cpu  # Force CPU mode

# Override camera filtering
uv run vbc /videos --camera "Sony,DJI"

# Override multiple settings
uv run vbc /videos \
  --config custom.yaml \
  --threads 8 \
  --cq 40 \
  --gpu \
  --skip-av1 \
  --clean-errors \
  --min-size 5242880 \
  --rotate-180 \
  --debug
```

## Environment-Specific Configs

You can maintain multiple config files:

```bash
# Production (high quality, slow)
uv run vbc /videos --config conf/production.yaml

# Fast preview (low quality, fast)
uv run vbc /videos --config conf/preview.yaml

# Archival (maximum quality)
uv run vbc /videos --config conf/archive.yaml
```

**Example `conf/archive.yaml`:**

```yaml
general:
  threads: 4
  cq: 35           # Very high quality
  gpu: false       # CPU for best quality
  copy_metadata: true
  use_exif: true
  min_compression_ratio: 0.05  # Must save at least 5%
```

## Validation

VBC uses **Pydantic** for config validation. Invalid settings will raise errors on startup:

```bash
# Invalid CQ (must be 0-63)
Error: cq must be between 0 and 63

# Invalid threads (must be > 0)
Error: threads must be greater than 0

# Invalid rotation angle
Error: Invalid rotation angle 45. Must be 0, 90, 180, or 270.
```

## Next Steps

- [Runtime Controls](../user-guide/runtime-controls.md) - Keyboard shortcuts
- [Advanced Features](../user-guide/advanced.md) - Dynamic CQ, auto-rotation
- [Architecture Overview](../architecture/overview.md) - How config is loaded
