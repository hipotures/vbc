# Utilities API

This page documents helper scripts and repair utilities under `vbc/utils/`.

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

## FLV Repair

High-level FLV repair wrapper for corrupted inputs.

::: vbc_legacy.utils.flv_repair
    options:
      show_source: true
      heading_level: 3

## FLV Repair Core

Byte-level helpers for finding FLV headers and copying repaired payloads.

::: vbc_legacy.utils.flv_repair_core
    options:
      show_source: true
      heading_level: 3

## Re-encode Repair

Fallback repair helper that re-encodes damaged files through ffmpeg.

::: vbc_legacy.utils.reencode_repair
    options:
      show_source: true
      heading_level: 3

## Audio Consistency Check

Command-line verifier for input/output audio codec handling.

::: vbc_legacy.utils.check_audio_consistency
    options:
      show_source: true
      heading_level: 3
