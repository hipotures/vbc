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

## Fix VBC Tags

Dry-run capable helper for adding missing VBC metadata tags to existing MP4 files.

::: vbc_legacy.utils.fix_vbc_tags
    options:
      show_source: true
      heading_level: 3

## Move Error Files

Move source MP4 files and matching `.err` markers into a quarantine directory.

::: vbc_legacy.utils.move_err_files
    options:
      show_source: true
      heading_level: 3

## Copy Failed Videos

Copy source videos that correspond to `.err` markers while preserving relative paths.

::: vbc_legacy.utils.copy_failed_videos
    options:
      show_source: true
      heading_level: 3

## Audio Consistency Check

Command-line verifier for input/output audio codec handling.

::: vbc_legacy.utils.check_audio_consistency
    options:
      show_source: true
      heading_level: 3
