# Utilities & Maintenance API

This page documents repair utilities under `vbc/utils/`, manifest maintenance services under `vbc/maintenance/`, and standalone helper utilities.

## FLV Repair

High-level FLV repair wrapper for corrupted inputs.

::: vbc.utils.flv_repair
    options:
      show_source: true
      heading_level: 3

## FLV Repair Core

Byte-level helpers for finding FLV headers and copying repaired payloads.

::: vbc.utils.flv_repair_core
    options:
      show_source: true
      heading_level: 3

## Re-encode Repair

Fallback repair helper that re-encodes damaged files through ffmpeg.

::: vbc.utils.reencode_repair
    options:
      show_source: true
      heading_level: 3

## Manifest Restore

Server-side and script helper for restoring failed producer manifests and moved sources (`vbc manifests restore`).

::: vbc.maintenance.restore
    options:
      show_source: true
      heading_level: 3

## Manifest Repair

Controlled recompression service for supported failed manifests (`vbc manifests repair`).

::: vbc.maintenance.repair
    options:
      show_source: true
      heading_level: 3

## Stand-alone helper scripts

These command-line helpers live in `scripts/` (not in the `vbc` package) and
work directly on folders, without a server. Usage and options:
`scripts/README.md`.

| Script | Purpose |
|---|---|
| `scripts/fix_vbc_tags.py` | Dry-run capable helper that adds missing VBC metadata tags to existing MP4 files |
| `scripts/move_err_files.py` | Move source files and matching `.err` markers into a quarantine directory |
| `scripts/copy_failed_videos.py` | Copy source videos that have `.err` markers, keeping relative paths |
| `scripts/check_audio_consistency.py` | Verify input/output audio codec handling |

