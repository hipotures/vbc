# Configuration vs CLI Analysis

This document compares the configuration parameters available in the YAML config file (`vbc/config/models.py`) versus the command-line interface (`vbc/main.py`).

## 1. Config-only Parameters (Missing in CLI)

These settings can **only** be modified via the YAML file.

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `prefetch_factor` | 1 | Queue multiplier for submit-on-demand. |
| `cpu_fallback` | False | Automatically retry on CPU after NVENC hardware errors. |
| `ffmpeg_cpu_threads` | null | Per-worker CPU limit for FFmpeg. |
| `copy_metadata` | True | Boolean flag to toggle metadata copying. |
| `use_exif` | True | Boolean flag to toggle ExifTool analysis. |
| `filter_cameras` | [] | List of camera models to process (also available via `--camera`). |
| `dynamic_cq` | {} | Mapping of camera models to specific CQ values. |
| `extensions` | [...] | List of allowed input file extensions. |
| `strip_unicode_display`| True | Clean UI filenames from special characters. |
| `min_compression_ratio`| 0.1 | Minimum required savings (also available via `--min-ratio`). |
| `gpu_config.*` | - | All advanced GPU monitoring settings (interval, window, device). |
| `ui.*` | - | All UI scaling and feed settings. |
| `autorotate.patterns` | {} | Regex-to-angle rotation mapping. |

## 2. CLI-only Parameters

These are operational flags not intended for persistent configuration.

| Flag | Description |
| :--- | :--- |
| `--config` / `-c` | Path to the YAML configuration file. |
| `--demo` | Enable simulation mode. |
| `--demo-config` | Path to the demo simulation YAML. |
| `--rotate-180` | Hardcoded rotation flag (Config supports 0, 90, 180, 270 via `manual_rotation`). |

## 3. Discrepancies and Bugs found in Code

During the comparison, the following issues were identified:

1.  **Ghost Arguments (FIXED)**: The documentation (`docs/user-guide/cli.md`) listed `--camera` and `--min-ratio` which were missing in code. **This has been fixed in v1.0.0.**
2.  **Rotation Inconsistency**: CLI only allows 180-degree rotation via `--rotate-180`, while the domain model supports any 90-degree increment.
3.  **Metadata/Exif toggles**: There is no way to disable metadata copying or Exif analysis via CLI flags (e.g., `--no-exif`).

## 4. Recommendations

*   **Fixed**: Implement `--camera` and `--min-ratio` in `vbc/main.py`.
*   **Improve**: Replace `--rotate-180` with `--rotate INT` to allow 90/270 overrides.
*   **Improve**: Add `--no-metadata` and `--no-exif` flags to CLI.
