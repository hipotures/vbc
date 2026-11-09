#!/usr/bin/env python3
"""Repair supported failed VBC manifests without republishing them to the queue."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from vbc.maintenance.restore import (
    RestoreError,
    _aligned_error_dirs,
    _deduplicated_enabled_entries,
    _manifest_source_error_package_dir,
    restore_failed_manifest,
    restore_failed_sources,
    rollback_restored_sources,
)
from vbc.config.loader import load_config
from vbc.config.models import AppConfig, InputDirEntry
from vbc.domain.models import CompressionManifest
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.infrastructure.ffmpeg import FFmpegAdapter
from vbc.infrastructure.ffprobe import FFprobeAdapter
from vbc.infrastructure.file_scanner import FileScanner
from vbc.pipeline.orchestrator import Orchestrator

MAX_FFMPEG_MEMORY_BYTES = 50 * 1024**3
MAX_ERROR_MARKER_BYTES = 1024 * 1024


class RepairError(RuntimeError):
    """Raised when a failed manifest cannot be repaired safely."""


@dataclass(frozen=True)
class RepairHandler:
    name: str
    description: str
    matches: Callable[[str], bool]
    drop_audio: bool = False


@dataclass(frozen=True)
class RepairCandidate:
    manifest_path: Path
    error_path: Path
    handler: RepairHandler | None
    error_text: str


@dataclass(frozen=True)
class RepairContext:
    metadata_dir: Path
    success_dir: Path
    error_dir: Path


@dataclass(frozen=True)
class RepairOutcome:
    manifest_path: Path
    status: str
    detail: str


def _matches_ffmpeg_244(error_text: str) -> bool:
    return bool(
        re.search(r"ffmpeg(?: concat)? exited with code 244\b", error_text, re.I)
        or re.search(r"(?:error(?: number)?|errno)\s*[:=]?\s*-12\b", error_text, re.I)
    )


REPAIR_HANDLERS = (
    RepairHandler(
        name="ffmpeg-244",
        description="FFmpeg 244/-12: recompress video without audio",
        matches=_matches_ffmpeg_244,
        drop_audio=True,
    ),
)


def _read_error_marker(error_path: Path) -> str:
    if error_path.is_symlink():
        raise RepairError(f"error marker cannot be a symlink: {error_path}")
    error_path = error_path.resolve(strict=True)
    if not error_path.is_file() or error_path.suffix.lower() != ".err":
        raise RepairError(f"expected an .err file: {error_path}")
    size = error_path.stat().st_size
    if size > MAX_ERROR_MARKER_BYTES:
        raise RepairError(
            f"error marker is too large ({size} bytes > {MAX_ERROR_MARKER_BYTES}): "
            f"{error_path}"
        )
    return error_path.read_text(encoding="utf-8", errors="replace")


def _handler_for(error_text: str) -> RepairHandler | None:
    return next(
        (handler for handler in REPAIR_HANDLERS if handler.matches(error_text)),
        None,
    )


def _candidate_from_path(path: Path) -> RepairCandidate:
    if path.suffix.lower() == ".err":
        error_path = path
        manifest_path = path.with_suffix(".json")
    elif path.suffix.lower() == ".json":
        manifest_path = path
        error_path = path.with_suffix(".err")
    else:
        raise RepairError(f"expected a .json or .err file: {path}")

    if manifest_path.is_symlink():
        raise RepairError(f"manifest cannot be a symlink: {manifest_path}")
    manifest_path = manifest_path.resolve(strict=True)
    if not manifest_path.is_file():
        raise RepairError(f"failed manifest is not a regular file: {manifest_path}")
    error_path = error_path.resolve(strict=True)
    error_text = _read_error_marker(error_path)
    return RepairCandidate(
        manifest_path=manifest_path,
        error_path=error_path,
        handler=_handler_for(error_text),
        error_text=error_text,
    )


def collect_candidates(target: Path) -> list[RepairCandidate]:
    """Collect one failed manifest or every .err descendant of a directory."""
    if target.is_symlink():
        raise RepairError(f"target cannot be a symlink: {target}")
    target = target.resolve(strict=True)
    if target.is_file():
        return [_candidate_from_path(target)]
    if not target.is_dir():
        raise RepairError(f"target is neither a file nor directory: {target}")

    candidates: list[RepairCandidate] = []
    for error_path in sorted(target.rglob("*.err")):
        try:
            candidates.append(_candidate_from_path(error_path))
        except (OSError, RepairError) as exc:
            candidates.append(
                RepairCandidate(
                    manifest_path=error_path.with_suffix(".json"),
                    error_path=error_path,
                    handler=None,
                    error_text=f"invalid entry: {exc}",
                )
            )
    return candidates


def _aligned_output_dirs(
    config: AppConfig,
    entries: list[InputDirEntry],
    original_indices: list[int],
) -> list[Path]:
    if config.output_dirs:
        raw_enabled_count = sum(entry.enabled for entry in config.input_dirs)
        if len(config.output_dirs) == len(entries):
            return [Path(path) for path in config.output_dirs]
        if len(config.output_dirs) == raw_enabled_count:
            enabled_positions = {
                config_index: enabled_index
                for enabled_index, config_index in enumerate(
                    index
                    for index, entry in enumerate(config.input_dirs)
                    if entry.enabled
                )
            }
            return [
                Path(config.output_dirs[enabled_positions[index]])
                for index in original_indices
            ]
        raise RepairError(
            "output_dirs count does not match enabled input_dirs in the configuration"
        )
    if config.suffix_output_dirs is None:
        raise RepairError(
            "configuration has neither output_dirs nor suffix_output_dirs"
        )
    return [
        Path(entry.path).with_name(
            f"{Path(entry.path).name}{config.suffix_output_dirs}"
        )
        for entry in entries
    ]


def resolve_repair_context(
    config: AppConfig,
    manifest_path: Path,
) -> RepairContext:
    try:
        manifest = CompressionManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise RepairError(f"invalid compression manifest: {exc}") from exc
    entries, original_indices = _deduplicated_enabled_entries(config)
    error_dirs = _aligned_error_dirs(config, entries, original_indices)
    output_dirs = _aligned_output_dirs(config, entries, original_indices)
    failed_parent = manifest_path.parent.resolve(strict=False)
    matches = [
        RepairContext(Path(entry.path), output_dir, error_dir)
        for entry, output_dir, error_dir in zip(
            entries,
            output_dirs,
            error_dirs,
            strict=True,
        )
        if entry.metadata
        and (
            error_dir.resolve(strict=False) == failed_parent
            or error_dir.resolve(strict=False) in failed_parent.parents
        )
    ]
    if not matches:
        source_error_package = _manifest_source_error_package_dir(
            manifest,
            config.suffix_errors_dirs,
        ).resolve(strict=False)
        if failed_parent == source_error_package:
            matches = [
                RepairContext(Path(entry.path), output_dir, error_dir)
                for entry, output_dir, error_dir in zip(
                    entries,
                    output_dirs,
                    error_dirs,
                    strict=True,
                )
                if entry.metadata
            ]
    if len(matches) != 1:
        raise RepairError(
            "failed manifest must map to exactly one enabled metadata input: "
            f"{failed_parent}; matches={len(matches)}"
        )
    return matches[0]


def _build_orchestrator(
    config: AppConfig,
    config_path: Path,
    context: RepairContext,
    event_bus: EventBus,
) -> tuple[Orchestrator, ExifToolAdapter]:
    repair_config = config.model_copy(deep=True)
    repair_config.general.threads = 1
    repair_config.general.verify_fail_action = "false"
    scanner = FileScanner(
        extensions=repair_config.general.extensions,
        min_size_bytes=repair_config.general.min_size_bytes,
    )
    exif = ExifToolAdapter()
    exif.et.run()
    orchestrator = Orchestrator(
        config=repair_config,
        event_bus=event_bus,
        file_scanner=scanner,
        exif_adapter=exif,
        ffprobe_adapter=FFprobeAdapter(),
        ffmpeg_adapter=FFmpegAdapter(
            event_bus=event_bus,
            memory_limit_bytes=MAX_FFMPEG_MEMORY_BYTES,
        ),
        output_dir_map={context.metadata_dir: context.success_dir},
        errors_dir_map={context.metadata_dir: context.error_dir},
        config_path=config_path,
        input_dir_entries={
            Path(entry.path): entry
            for entry in repair_config.input_dirs
            if entry.enabled
        },
    )
    return orchestrator, exif


def repair_candidate(
    candidate: RepairCandidate,
    config_path: Path,
    *,
    dry_run: bool = False,
    event_bus: EventBus | None = None,
) -> RepairOutcome:
    if candidate.handler is None:
        detail = candidate.error_text.strip().splitlines()[0] or "unsupported error"
        return RepairOutcome(candidate.manifest_path, "SKIPPED", detail)

    try:
        config = load_config(config_path)
        context = resolve_repair_context(config, candidate.manifest_path)
        manifest = CompressionManifest.model_validate_json(
            candidate.manifest_path.read_text()
        )
        restore_plan = restore_failed_manifest(
            candidate.manifest_path,
            config_path,
            dry_run=True,
        )
    except (OSError, ValueError, RestoreError, RepairError) as exc:
        return RepairOutcome(candidate.manifest_path, "FAILED", str(exc))

    if dry_run:
        source_count = len(restore_plan.restored_sources) + len(
            restore_plan.already_present_sources
        )
        return RepairOutcome(
            candidate.manifest_path,
            "READY",
            f"{candidate.handler.description}; {source_count} source(s)",
        )

    restored = ()
    exif: ExifToolAdapter | None = None
    try:
        restored = restore_failed_sources(restore_plan)
        if event_bus is None:
            event_bus = EventBus()
        orchestrator, exif = _build_orchestrator(
            config,
            config_path,
            context,
            event_bus,
        )
        video_file = orchestrator.prepare_metadata_repair(
            candidate.manifest_path,
            context.success_dir,
            context.error_dir,
        )
        if video_file is not None:
            orchestrator.process_metadata_repair(
                video_file,
                drop_audio=candidate.handler.drop_audio,
            )

        success_manifest = (
            context.success_dir
            / manifest.date_partition
            / candidate.manifest_path.name
        )
        if not success_manifest.is_file():
            raise RepairError("VBC did not route the manifest to the success directory")
        if not candidate.error_path.is_file():
            raise RepairError("original .err marker disappeared")
        return RepairOutcome(
            success_manifest,
            "REPAIRED",
            "video recompressed without audio; .err retained",
        )
    except KeyboardInterrupt:
        if restored:
            rollback_restored_sources(restored)
        raise
    except (OSError, ValueError, RestoreError, RepairError, RuntimeError) as exc:
        try:
            if restored:
                rollback_restored_sources(restored)
        except RestoreError as rollback_exc:
            return RepairOutcome(
                candidate.manifest_path,
                "FAILED",
                f"{exc}; {rollback_exc}",
            )
        return RepairOutcome(candidate.manifest_path, "FAILED", str(exc))
    finally:
        if exif is not None and exif.et.running:
            exif.et.terminate()
