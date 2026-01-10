# Advanced Features

This guide covers VBC's advanced features: dynamic CQ, auto-rotation, camera filtering, and more.

## Dynamic CQ (Camera-Specific Quality)

Different cameras produce different quality levels. VBC can apply custom CQ values per camera model.

### Configuration

```yaml
# conf/vbc.yaml
gpu_encoder:
  common_args:
    - "-cq 45"  # Default for unknown cameras

general:
  dynamic_cq:
    "ILCE-7RM5": 38      # Sony A7R V - very high quality
    "DC-GH7": 40         # Panasonic GH7 - high quality
    "DJI OsmoPocket3": 48  # DJI Pocket 3 - standard quality
```

### How It Works

1. **ExifTool analysis**: VBC extracts full EXIF metadata
2. **Pattern matching**: Searches all metadata for camera model strings
3. **Quality override**: If match found, uses custom value instead of default
4. **First match wins**: Patterns are checked in config file order

### Example

```yaml
dynamic_cq:
  "Sony": 40        # Matches all Sony cameras
  "ILCE-7RM5": 38   # Specific model (higher priority if listed first)
```

**File:** `IMG_1234.MOV`
**EXIF:** `EXIF:Model = "ILCE-7RM5"`
**Result:** CQ = 38 (specific match beats generic)

### CLI Override

```bash
# Override all CQ (disables dynamic CQ)
uv run vbc /videos --quality 40

# Dynamic CQ still active (from config)
uv run vbc /videos --config conf/vbc.yaml
```

**Note:** `--quality` flag disables dynamic CQ entirely.

### Debugging

Enable debug logging to see CQ decisions:

```bash
uv run vbc /videos --debug
```

Look for:
```
Detected camera: ILCE-7RM5 - using custom CQ: 38
```

## Auto-Rotation

Automatically rotate videos based on filename patterns (useful for GoPro, drone footage).

### Configuration

```yaml
# conf/vbc.yaml
autorotate:
  patterns:
    "DJI_.*\\.MP4": 0        # DJI drones - no rotation
    "GOPR\\d+\\.MP4": 180    # GoPro pattern - flip 180°
    "IMG_\\d{4}\\.MOV": 90   # iPhone - rotate 90°
```

**Regex syntax:** Python `re` module (backslashes must be escaped).

### How It Works

1. **Filename check**: VBC checks each filename against all patterns
2. **First match**: Uses angle from first matching pattern
3. **Rotation filter**: Applies FFmpeg transpose/hflip+vflip filters

### Rotation Angles

| Angle | FFmpeg Filter |
|-------|---------------|
| 0     | None (no rotation) |
| 90    | `transpose=1` (clockwise) |
| 180   | `hflip,vflip` (upside down) |
| 270   | `transpose=2` (counter-clockwise) |

### Manual Override

```bash
# Rotate all videos 180° (overrides config patterns)
uv run vbc /videos --rotate-180
```

## Camera Filtering

Process only files from specific camera models.

### Configuration

```yaml
# conf/vbc.yaml
general:
  filter_cameras:
    - "Sony"
    - "DJI OsmoPocket3"
    - "ILCE-7RM5"
```

Or via CLI:

```bash
uv run vbc /videos --camera "Sony,DJI"
```

### How It Works

1. **EXIF extraction**: Uses ExifTool to get camera model
2. **Substring match**: Checks if any filter string is in camera model
3. **Skip non-matches**: Files from other cameras are skipped

**Example:**
```
Filter: ["Sony", "DJI"]

File: IMG_1234.MOV
Camera: ILCE-7RM5 (Sony A7R V)
Match: "Sony" in "ILCE-7RM5" → Process ✓

File: VIDEO_5678.MP4
Camera: Canon EOS R5
Match: None → Skip ✗
```

## Minimum Compression Ratio

Keep original file if compression savings are below threshold.

### Configuration

```yaml
# conf/vbc.yaml
general:
  min_compression_ratio: 0.1  # Require 10% savings
```

Or via CLI:

```bash
uv run vbc /videos --min-ratio 0.2  # 20% minimum
```

### How It Works

1. **Compress normally**: FFmpeg creates compressed file
2. **Check ratio**: Calculate `(1 - output_size/input_size)`
3. **Compare threshold**: If savings < `min_compression_ratio`:
   - Delete compressed file
   - Copy original to output directory
4. **Log decision**: "MinRatio: kept original (X% < Y% minimum)"

**Example:**

```
Input:  100 MB
Output:  92 MB
Savings: 8% (1 - 92/100 = 0.08)

min_compression_ratio: 0.1 (10%)
Result: 8% < 10% → Keep original (copy 100 MB file)
```

### Use Cases

- **Already compressed**: Files from efficient cameras (e.g., DJI with H.265)
- **Tiny files**: Small clips where AV1 overhead > savings
- **Quality priority**: Never accept worse compression

## Skip AV1 Files

Avoid re-compressing files already in AV1 codec.

### Configuration

```yaml
# conf/vbc.yaml
general:
  skip_av1: true
```

Or via CLI:

```bash
uv run vbc /videos --skip-av1
```

### How It Works

1. **FFprobe check**: Extract codec from stream info
2. **AV1 detection**: Check if `codec == "av1"`
3. **Skip**: Don't queue for compression

**Use case:** Mixed library with some files already compressed to AV1.

## Audio Consistency Check

Verify that audio handling in the output matches VBC's rules.

Behavior summary:
- Lossless (`pcm_*`, `flac`, `alac`, `truehd`, `mlp`, `wavpack`, `ape`, `tta`) → AAC 256 kbps
- AAC/MP3 → stream copy
- Other/unknown → AAC 192 kbps
- No audio → no audio

### Usage

```bash
# Default output dir: <input_dir>_out
python vbc/utils/check_audio_consistency.py /path/to/videos

# Custom output dir
python vbc/utils/check_audio_consistency.py /path/to/videos --output-dir /path/to/videos_out
```

### Output

The script prints summary counts (including how many input files had no audio),
lists missing outputs, and reports any codec/bitrate mismatches.

## Prefetch Factor

Controls submit-on-demand queue size.

### Configuration

```yaml
# conf/vbc.yaml
general:
  prefetch_factor: 2  # Queue 2x threads
```

**Formula:**
```
max_queued_jobs = prefetch_factor × current_threads
```

**Examples:**
- `prefetch_factor=1, threads=4` → 4 jobs queued
- `prefetch_factor=2, threads=4` → 8 jobs queued
- User presses `>` (threads 4→5) → queue expands to 10 jobs

### Trade-offs

| Factor | Pros | Cons |
|--------|------|------|
| 1 (default) | Minimal memory, responsive to thread changes | Less parallelism |
| 2-3 | Better parallelism, smoother queue | Higher memory usage |
| 4-5 | Maximum parallelism | Memory intensive, slow thread response |

**Recommendation:** Keep at 1 unless you have 16+ threads.

## Deep Metadata Copy

VBC uses ExifTool to preserve all metadata including GPS, camera settings, and custom tags.

### Configuration

```yaml
# conf/vbc.yaml
general:
  copy_metadata: true  # Default
  use_exif: true       # Required for deep copy
```

### What's Copied

- **GPS**: Latitude, longitude, altitude
- **Camera**: Model, lens, focal length, ISO, aperture
- **XMP**: All XMP tags (Adobe, vendor-specific)
- **QuickTime**: All QuickTime metadata
- **Custom VBC tags**: Original filename, size, quality, encoder, timestamp

### ExifTool Config

VBC uses `conf/exiftool.conf` to define custom VBC tags:

```perl
# conf/exiftool.conf
%Image::ExifTool::UserDefined = (
    'Image::ExifTool::XMP::Main' => {
        VBC => {
            SubDirectory => {
                TagTable => 'Image::ExifTool::UserDefined::VBC',
            },
        },
    },
);

%Image::ExifTool::UserDefined::VBC = (
    GROUPS => { 0 => 'XMP', 1 => 'XMP-vbc', 2 => 'Image' },
    NAMESPACE => { 'VBC' => 'http://ns.example.com/vbc/1.0/' },
    WRITABLE => 'string',
    VBCOriginalName => { },
    VBCOriginalSize => { },
    VBCQuality => { },
    VBCEncoder => { },
    VBCFinishedAt => { },
);
```

### View VBC Tags

```bash
exiftool -XMP-vbc:all compressed.mp4
```

Output:
```
VBC Original Name       : original_video.mp4
VBC Original Size       : 125829120
VBC Quality             : 45
VBC Encoder             : NVENC AV1 (GPU)
VBC Finished At         : 2025-12-21T15:30:45+01:00
```

## Hardware Capability Detection

VBC automatically detects GPU limitations including:
- 10-bit AV1 encoding not supported (older GPUs)
- NVENC session limits exceeded (too many concurrent encodes)

### Common HW_CAP Causes

**Session Limits Exceeded:**
- RTX 30-series: Max ~5 concurrent sessions
- RTX 40-series (e.g., 4090): Max 10-12 concurrent sessions
- If you run `--threads 16` on RTX 4090, threads 13+ will get HW_CAP errors

**10-bit Encoding:**
- Older GPUs don't support 10-bit AV1
- RTX 40-series has full 10-bit support

### Error Detection

FFmpeg outputs:
```
Hardware is lacking required capabilities
```

VBC:
1. Detects error string or exit code 187
2. Sets job status to `HW_CAP_LIMIT`
3. Creates `.err` marker
4. Increments `hw_cap` counter in UI

### UI Display

```
┌─ COMPRESSION STATUS ───────────────────────────┐
│ Ignored: size:5 | err:2 | hw_cap:3 | av1:0    │
└────────────────────────────────────────────────┘

┌─ SUMMARY ──────────────────────────────────────┐
│ ✓ 42 success  ✗ 2 errors  ⚠ 3 hw_cap  ⊘ 0 skip│
└────────────────────────────────────────────────┘
```

### Workarounds

**Option 1:** Reduce thread count to GPU session limit
```bash
# RTX 30-series
uv run vbc /videos --threads 5

# RTX 4090
uv run vbc /videos --threads 12
```

**Option 2:** Use CPU mode (no hardware limitations)
```bash
uv run vbc /videos --cpu
```

**Option 3:** Reduce quality for 10-bit issues
```bash
uv run vbc /videos --quality 38
```

**Option 4:** Upgrade GPU (RTX 40-series has full 10-bit AV1 support and higher session limits)

## Color Space Fix (FFmpeg 7.x)

FFmpeg 7.x has issues with "reserved" color space. VBC automatically fixes this.

### Detection

FFprobe shows:
```
color_space=reserved
```

### Fix Process

1. **Remux**: Use bitstream filter to set valid color space
   ```bash
   ffmpeg -i input.mp4 -c copy \
     -bsf:v hevc_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1 \
     temp_colorfix.mp4
   ```

2. **Compress**: Use fixed file as input

3. **Cleanup**: Delete temp file

### Codecs Supported

- **HEVC** (h265): `hevc_metadata` filter
- **H.264**: `h264_metadata` filter
- **Others**: Proceed without fix (warning logged)

### Debug Logging

```bash
uv run vbc /videos --debug
```

Look for:
```
Detected reserved color space in video.mp4, applying fix...
Successfully fixed color space for video.mp4
```

## Unicode Handling

Filenames with emoji or special Unicode can break table alignment.

### Configuration

```yaml
# conf/vbc.yaml
general:
  strip_unicode_display: true  # Default
```

**Effect:**
- **Filesystem**: Filenames unchanged
- **UI display**: Non-ASCII replaced with `?`

**Example:**
```
File on disk: video_🎬_final.mp4
UI display:   video_?_final.mp4
```

**Disable:**
```yaml
strip_unicode_display: false
```

**Warning:** May cause table misalignment in UI.

## Next Steps

- [CLI Reference](cli.md) - All command-line options
- [Runtime Controls](runtime-controls.md) - Keyboard shortcuts
- [Architecture](../architecture/overview.md) - How features work internally
