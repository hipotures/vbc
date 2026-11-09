"""Settings-tab lines built from structured ``/config`` + ``/io``.

The legacy TUI got these strings preformatted from vbc_legacy/main.py; now the
server sends data and the client formats it (same wording as legacy).
"""

from __future__ import annotations

from datetime import datetime
from typing import List

from vbc import contracts as dto
from vbc.clients import format as fmt


def _rule(rule: dict) -> str:
    rate = rule.get("rate")
    if not rate:
        return f"cq={rule.get('cq')}"
    parts = [f"cq={rule.get('cq')}", f"bps={rate.get('bps')}"]
    if rate.get("minrate") is not None:
        parts.append(f"minrate={rate['minrate']}")
    if rate.get("maxrate") is not None:
        parts.append(f"maxrate={rate['maxrate']}")
    return ", ".join(parts)


def build_config_lines(view: dto.ConfigView, io: dto.IoView) -> List[str]:
    s = view.sections
    general, encoder, quality = s.get("general", {}), s.get("encoder", {}), s.get("quality", {})
    metadata, autorotate, demo = s.get("metadata", {}), s.get("autorotate", {}), s.get("demo", {})
    session = s.get("session", {})

    dynamic = quality.get("dynamic_quality") or {}
    dynamic_info = ", ".join(f"{k}:({_rule(v)})" for k, v in dynamic.items()) if dynamic else "None"
    cameras = ", ".join(general.get("filter_cameras") or []) or "None"
    manual = general.get("manual_rotation")
    manual_rotation = f"{manual}°" if manual is not None else "None"
    if general.get("queue_sort") == "rand" and general.get("queue_seed") is not None:
        queue_sort = f"rand (seed {general['queue_seed']})"
    else:
        queue_sort = general.get("queue_sort")
    use_exif, copy_metadata = general.get("use_exif"), general.get("copy_metadata")
    method = "Deep (ExifTool + XMP)" if (use_exif and copy_metadata) else ("Basic (FFmpeg)" if copy_metadata else "None")
    extensions = demo.get("extensions") if demo.get("enabled") and demo.get("extensions") else general.get("extensions", [])
    ext_list = ", ".join(e if e.startswith(".") else f".{e}" for e in extensions)
    started = session.get("started_at")
    start_text = datetime.fromtimestamp(started / 1000).strftime("%Y-%m-%d %H:%M:%S") if started else "-"

    enabled_dirs = [d for d in io.dirs if d.enabled]
    dir_lines = []
    for index, d in enumerate(enabled_dirs, start=1):
        ok = d.exists and d.readable
        icon = "[green]✓ [/]" if ok else ("[red]✗ [/]" if not d.exists else "[red]⚡[/]")
        dir_lines.append(f"  {icon}{index}. {d.path}")

    title = "Video Batch Compression - demo" if demo.get("enabled") else f"Video Batch Compression - {encoder.get('label')}"
    lines = [
        title,
        f"Start: {start_text}",
        f"Input folders: {len(enabled_dirs)}",
        *dir_lines,
        f"Threads: {general.get('threads')} (Prefetch: {general.get('prefetch_factor')}x)",
        f"Worker preflight: {general.get('preflight_in_worker')}",
        f"Encoder: {encoder.get('label')} | Preset: {encoder.get('preset') or '—'}",
        "Audio: Auto (lossless->AAC 256k, AAC/MP3 copy, other->AAC 192k)",
        f"Quality: {quality.get('display') or 'unknown'} (Default)",
        f"Dynamic Quality: {dynamic_info}",
        f"Camera Filter: {cameras}",
        f"Video metadata: {method} (Analysis: {use_exif})",
    ]
    if not demo.get("enabled"):
        lines.append(
            f"Manifest audio-only: {metadata.get('audio_only')} | "
            f"Max dropped frames: {metadata.get('max_dropped_frames')}"
        )
    lines += [
        f"Autorotate: {len((autorotate.get('patterns') or {}))} rules loaded",
        f"Manual Rotation: {manual_rotation}",
        f"Extensions: {ext_list} → {encoder.get('output_suffix')}",
        f"Queue sort: {queue_sort}",
        f"CPU fallback: {general.get('cpu_fallback')} | CPU threads per worker: {general.get('ffmpeg_cpu_threads') or 'auto'}",
        f"Min size: {fmt.size(general.get('min_size_bytes'))} | Skip AV1: {general.get('skip_av1')}",
        f"Verification mode: {general.get('verify_fail_action')}",
    ]
    if demo.get("enabled") and "files" in demo:
        lines += [
            f"Demo files: {demo['files']} | Errors: {demo['errors']} | Kept original: {demo['kept_original']}",
            f"Demo sizes: {demo['size_min_mb']:.0f}-{demo['size_max_mb']:.0f} MB ({demo['size_distribution']})",
            f"Demo speed: {demo['throughput_mb_s']:.1f} MB/s (±{int(demo['jitter_pct'] * 100)}%)",
        ]
    lines += [
        f"Auto repair errors: {general.get('auto_repair_errors')}",
        f"Source after success: {general.get('source_policy')} | "
        f"Manifest sources: {metadata.get('source_policy') or 'per manifest'}",
        f"Clean errors: {general.get('clean_errors')} | Strip Unicode: {general.get('strip_unicode_display')}",
        f"Debug logging: {general.get('debug')}",
    ]
    return lines
