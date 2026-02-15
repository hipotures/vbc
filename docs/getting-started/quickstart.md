# Quick Start

This guide walks you through your first video compression job with VBC.

## Basic Compression

### Step 1: Prepare Your Videos

Create a directory with video files:

```bash
mkdir ~/Videos/raw
# Copy some .mp4, .mov, or .avi files to this directory
```

### Step 2: Run VBC

```bash
cd ~/DEV/vbc
uv run vbc ~/Videos/raw
```

VBC will:

1. Scan `~/Videos/raw/` for video files
2. Create output directory `~/Videos/raw_out/`
3. Start compressing with default settings (quality from encoder args, 1 thread)
4. Show real-time progress in interactive dashboard

### Step 3: Monitor Progress

The dashboard shows:

```
┌─ MENU ─────────────────────────────────────────┐
│ < decrease threads | > increase threads | ...   │
└────────────────────────────────────────────────┘
┌─ COMPRESSION STATUS ───────────────────────────┐
│ Files to compress: 12 | Already compressed: 0  │
│ Total: 12 files | Threads: 4 | ...            │
└────────────────────────────────────────────────┘
┌─ CURRENTLY PROCESSING ─────────────────────────┐
│ ● video1.mp4    8M  60fps  120.5MB  00:15      │
│ ○ video2.mov    4M  30fps   85.2MB  00:08      │
└────────────────────────────────────────────────┘
```

### Step 4: Use Runtime Controls

While running, press:

- **`.`** - Increase threads (max 16)
- **`,`** - Decrease threads (min 1)
- **`S`** - Graceful shutdown (finish current jobs)
- **`R`** - Refresh queue (scan for new files)

### Step 5: Check Results

```bash
ls -lh ~/Videos/raw_out/
# Compressed .mp4 files
# *.err - error markers (if any failures)
ls -lh /tmp/vbc/compression.log
# detailed log
```

## Common Use Cases

### GPU Acceleration (NVENC)

For NVIDIA GPUs (faster, lower quality ceiling):

```bash
uv run vbc ~/Videos/raw --gpu --threads 8
```

!!! tip "GPU vs CPU"
    - **GPU (NVENC)**: Fast, good for 1080p/1440p, max quality ~CQ35-40
    - **CPU (SVT-AV1)**: Slower, excellent quality at any resolution, archival use

### High Quality Archive

For maximum quality (slower):

```bash
uv run vbc ~/Videos/raw --cpu --quality 35 --threads 4
```

Lower quality value (CQ/CRF) = higher quality (range: 0-63)

### Camera-Specific Settings

If you have videos from specific cameras (e.g., Sony, DJI):

```bash
# First, create conf/vbc.yaml with dynamic_quality settings
# Then run with camera filtering
uv run vbc ~/Videos/raw --camera "Sony,DJI"
```

This will only process files from those cameras and apply custom CQ per camera.

### Rotate Videos

For upside-down drone footage:

```bash
uv run vbc ~/Videos/raw --rotate-180
```

Or use auto-rotation patterns in `conf/vbc.yaml` (regex-based).

## Configuration File

Create `conf/vbc.yaml` for persistent settings:

```yaml
general:
  threads: 8
  gpu: true
  copy_metadata: true
  use_exif: true
  extensions: [".mp4", ".mov", ".avi", ".flv"]
  min_size_bytes: 1048576  # 1 MiB
  # Camera-specific quality
  dynamic_quality:
    "ILCE-7RM5": 38      # Sony A7R V
    "DC-GH7": 40         # Panasonic GH7
    "DJI OsmoPocket3": 45

  # Camera filtering (empty = all cameras)
  filter_cameras: []

gpu_encoder:
  common_args:
    - "-cq 42"

autorotate:
  patterns:
    "DJI_.*\\.MP4": 0      # DJI drones - no rotation
    "GOPR.*\\.MP4": 180    # GoPro specific pattern
```

Then run:

```bash
uv run vbc ~/Videos/raw --config conf/vbc.yaml
```

CLI arguments override config file settings.

## Output Structure

```
~/Videos/raw/              # Input directory
├── video1.mp4
├── video2.mov
└── subfolder/
    └── video3.avi

~/Videos/raw_out/          # Output directory (created automatically)
├── video1.mp4             # Compressed
├── video2.mp4             # Compressed (converted to output format; mp4 by default)
├── subfolder/
│   └── video3.mp4         # Compressed
└── video_error.err        # Error marker (if compression failed)

/tmp/vbc/compression.log    # Detailed log
```

By default, outputs are `.mp4`. If encoder args include `-f matroska` or `-f mov`, output extensions follow the selected format.

## Next Steps

- [Configuration Guide](configuration.md) - Deep dive into all settings
- [Runtime Controls](../user-guide/runtime-controls.md) - Master keyboard shortcuts
- [Advanced Features](../user-guide/advanced.md) - Dynamic Quality, auto-rotation, filtering
