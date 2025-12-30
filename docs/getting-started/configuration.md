# Configuration

VBC can be configured via YAML files and CLI arguments. CLI arguments always override config file settings.

## Configuration File

Default location: `conf/vbc.yaml`

## Demo Configuration

Demo mode uses a separate simulation file: `conf/demo.yaml`.

```bash
uv run vbc/main.py --demo --demo-config conf/demo.yaml
```

This file controls simulated file counts, size distribution, processing speed, and error mix.

### Full Example

```yaml
input_dirs:
  - /path/to/videos
  - /path/to/folder with spaces

general:
  # === Core Settings ===
  threads: 8                    # Max concurrent compression threads (1-16)
  cq: 45                        # Default constant quality (0-63, lower=better)
  prefetch_factor: 1            # Submit-on-demand multiplier (1-5)
  gpu: true                     # Use GPU (NVENC) vs CPU (SVT-AV1)
  queue_sort: name              # Queue order: name, rand, dir, size, size-asc, size-desc, ext
  queue_seed: null              # Optional seed for deterministic rand order
  log_path: /tmp/vbc/compression.log  # Log file location

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

autorotate:
  patterns:                     # Regex -> Rotation angle
    "DJI_.*\\.MP4": 0          # DJI drones - no rotation
    "GOPR\\d+\\.MP4": 180      # GoPro pattern - 180° flip
    "IMG_\\d{4}\\.MOV": 90     # iPhone pattern - 90° rotation
```

## Configuration Reference

### General Settings

#### `threads`
- **Type**: Integer (1-16)
- **Default**: 4
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

#### `log_path`
- **Type**: String or null
- **Default**: `/tmp/vbc/compression.log`
- **Description**: Path to log file (overrides the output directory default)

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
uv run vbc/main.py /videos --threads 16 --cq 38

# Override GPU setting
uv run vbc/main.py /videos --cpu  # Force CPU mode

# Override camera filtering
uv run vbc/main.py /videos --camera "Sony,DJI"

# Override multiple settings
uv run vbc/main.py /videos \
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
uv run vbc/main.py /videos --config conf/production.yaml

# Fast preview (low quality, fast)
uv run vbc/main.py /videos --config conf/preview.yaml

# Archival (maximum quality)
uv run vbc/main.py /videos --config conf/archive.yaml
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
