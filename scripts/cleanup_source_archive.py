#!/usr/bin/env python3
"""Verify archived sources against compressed outputs and delete explicit matches."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from vbc.config.loader import load_config
from vbc.config.models import AppConfig
from vbc.infrastructure.ffprobe import FFprobeAdapter

_PART_SUFFIX = re.compile(r"^(?P<base>.+)_part(?P<number>\d+)$", re.I)
_TAG_BATCH_SIZE = 200
_QUARANTINE_STATUSES = {
    "CORRUPT_INPUT",
    "CORRUPT_MOOV",
    "FFMPEG_SIGABRT",
    "FFMPEG_SIGSEGV",
    "HARDWARE_UNSUPPORTED",
}
_VIDEO_EXTENSIONS = {
    ".avi",
    ".flv",
    ".m2ts",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".webm",
}

ProgressCallback = Callable[[str, int, int], None]
_ROOT_INFERENCE_SAMPLE_SIZE = 100


def _format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


class SourceCleanupError(RuntimeError):
    """Raised when source archive verification cannot proceed safely."""


def _infer_compressed_root(config: AppConfig) -> Path:
    manifest_paths: list[tuple[int, Path]] = []
    for entry in config.input_dirs:
        if not entry.enabled or not entry.metadata:
            continue
        metadata_dir = Path(entry.path)
        if not metadata_dir.is_dir():
            continue
        for path in metadata_dir.glob("*.json"):
            try:
                if path.is_file() and not path.is_symlink():
                    manifest_paths.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
    manifest_paths.sort(key=lambda item: item[0], reverse=True)

    roots: set[Path] = set()
    for _, manifest_path in manifest_paths[:_ROOT_INFERENCE_SAMPLE_SIZE]:
        try:
            payload = json.loads(manifest_path.read_text())
            output_path = Path(payload["output_path"])
            username = str(payload["producer"]["username"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if not output_path.is_absolute() or output_path.parent.name != username:
            continue
        roots.add(output_path.parent.parent)

    if not roots:
        raise SourceCleanupError(
            "compressed directory is not configured directly and could not be "
            "inferred from current metadata manifests"
        )
    if len(roots) != 1:
        rendered = ", ".join(str(path) for path in sorted(roots))
        raise SourceCleanupError(
            f"metadata manifests reference multiple compressed roots: {rendered}"
        )
    return roots.pop()


def _resolve_cli_settings(
    source_archive: Path | None,
    compressed_dir: Path | None,
    min_size_bytes: int | None,
    config_path: Path,
) -> tuple[Path, Path, int, AppConfig]:
    try:
        config = load_config(config_path)
    except Exception as exc:
        raise SourceCleanupError(f"failed to load VBC config {config_path}: {exc}") from exc

    if source_archive is None:
        configured_source = config.metadata.move_after_success_dir
        if not configured_source:
            raise SourceCleanupError(
                "source archive was not supplied and metadata.move_after_success_dir "
                "is not configured"
            )
        source_archive = Path(configured_source)
    if compressed_dir is None:
        compressed_dir = _infer_compressed_root(config)
    if min_size_bytes is None:
        min_size_bytes = config.general.min_size_bytes
    return source_archive, compressed_dir, min_size_bytes, config


def _metadata_success_dirs(config: AppConfig) -> tuple[Path, ...]:
    enabled_entries = [entry for entry in config.input_dirs if entry.enabled]
    directories: list[Path] = []
    for index, entry in enumerate(enabled_entries):
        if not entry.metadata:
            continue
        if config.output_dirs:
            directories.append(Path(config.output_dirs[index]))
        elif config.suffix_output_dirs is not None:
            input_path = Path(entry.path)
            directories.append(
                input_path.with_name(f"{input_path.name}{config.suffix_output_dirs}")
            )
    return tuple(directories)


def _metadata_error_dirs(config: AppConfig) -> tuple[Path, ...]:
    enabled_entries = [entry for entry in config.input_dirs if entry.enabled]
    directories: list[Path] = []
    for index, entry in enumerate(enabled_entries):
        if not entry.metadata:
            continue
        if config.errors_dirs:
            directories.append(Path(config.errors_dirs[index]))
        elif config.suffix_errors_dirs is not None:
            input_path = Path(entry.path)
            directories.append(
                input_path.with_name(f"{input_path.name}{config.suffix_errors_dirs}")
            )
    return tuple(directories)


def _metadata_search_dirs(config: AppConfig) -> tuple[Path, ...]:
    input_dirs = tuple(
        Path(entry.path)
        for entry in config.input_dirs
        if entry.enabled and entry.metadata
    )
    return tuple(
        dict.fromkeys(
            (*input_dirs, *_metadata_success_dirs(config), *_metadata_error_dirs(config))
        )
    )


def _completed_recording_ids(config: AppConfig) -> set[str]:
    recording_ids: set[str] = set()
    for directory in _metadata_success_dirs(config):
        if not directory.is_dir():
            continue
        for manifest_path in directory.glob("ttracker-*.json"):
            if manifest_path.is_file() and not manifest_path.is_symlink():
                recording_ids.add(manifest_path.stem.removeprefix("ttracker-"))
    return recording_ids


@dataclass(frozen=True)
class SourceGroup:
    relative_dir: Path
    output_name: str
    sources: tuple[tuple[int, Path], ...]


@dataclass(frozen=True)
class SourceDecision:
    source_path: Path
    output_path: Path
    part_number: int
    status: str
    detail: str
    evidence_outputs: tuple[Path, ...] = ()
    size_bytes: int = 0
    quarantine_path: Path | None = None
    related_metadata_paths: tuple[Path, ...] = ()

    @property
    def verified(self) -> bool:
        return self.status in {"VERIFIED", "LEGACY_MATCH"}

    @property
    def deletion_eligible(self) -> bool:
        return (
            self.verified
            or self.status
            in {
                "BELOW_MIN_SIZE",
                "DONE_BELOW_MIN_SIZE",
                "DONE_NO_VIDEO",
                "IGNORED_NO_VIDEO",
            }
            or (
                self.status in _QUARANTINE_STATUSES
                and self.quarantine_path is not None
            )
        )


@dataclass
class CleanupResult:
    decisions: list[SourceDecision] = field(default_factory=list)
    deleted: int = 0
    would_delete: int = 0
    failed: int = 0
    symlinks_ignored: int = 0
    non_video_ignored: int = 0
    tag_scan_warning: str | None = None
    min_size_bytes: int | None = None
    delete_limit: int | None = None
    deleted_paths: list[Path] = field(default_factory=list)
    would_delete_paths: list[Path] = field(default_factory=list)
    quarantined: int = 0
    quarantined_paths: list[Path] = field(default_factory=list)
    would_quarantine: int = 0
    would_quarantine_paths: list[Path] = field(default_factory=list)


def _source_identity(path: Path) -> tuple[str, int]:
    match = _PART_SUFFIX.fullmatch(path.stem)
    if match is None:
        return path.stem, 1
    return match.group("base"), int(match.group("number"))


def collect_source_groups(source_root: Path) -> tuple[list[SourceGroup], int, int]:
    """Group regular archive files by their normalized output path."""
    if source_root.is_symlink():
        raise SourceCleanupError(f"source archive cannot be a symlink: {source_root}")
    source_root = source_root.resolve(strict=True)
    if not source_root.is_dir():
        raise SourceCleanupError(f"not a directory: {source_root}")

    grouped: dict[tuple[Path, str], list[tuple[int, Path]]] = defaultdict(list)
    symlinks_ignored = 0
    non_video_ignored = 0
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            symlinks_ignored += 1
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in _VIDEO_EXTENSIONS:
            non_video_ignored += 1
            continue
        relative = path.relative_to(source_root)
        base, part_number = _source_identity(path)
        output_name = f"{base}{path.suffix}"
        grouped[(relative.parent, output_name)].append((part_number, path))

    groups: list[SourceGroup] = []
    for (relative_dir, output_name), sources in sorted(
        grouped.items(), key=lambda item: (str(item[0][0]), item[0][1])
    ):
        groups.append(
            SourceGroup(
                relative_dir=relative_dir,
                output_name=output_name,
                sources=tuple(sorted(sources)),
            )
        )
    return groups, symlinks_ignored, non_video_ignored


def _output_family(base_output: Path) -> tuple[Path, ...]:
    if base_output.is_symlink() or not base_output.is_file():
        return ()
    pattern = re.compile(
        rf"^{re.escape(base_output.stem)}_(\d+){re.escape(base_output.suffix)}$"
    )
    numbered: list[tuple[int, Path]] = []
    for path in base_output.parent.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        match = pattern.fullmatch(path.name)
        if match is not None:
            numbered.append((int(match.group(1)), path))
    return (base_output, *(path for _, path in sorted(numbered)))


def _has_tag(entry: dict[str, object], name: str) -> bool:
    normalized_name = re.sub(r"[^a-z0-9]", "", name.lower())
    return any(
        re.sub(r"[^a-z0-9]", "", key.split(":")[-1].lower()) == normalized_name
        and value not in (None, "")
        for key, value in entry.items()
    )


def _tag_value(entry: dict[str, object], name: str) -> object | None:
    normalized_name = re.sub(r"[^a-z0-9]", "", name.lower())
    for key, value in entry.items():
        if re.sub(r"[^a-z0-9]", "", key.split(":")[-1].lower()) == normalized_name:
            return value
    return None


def _read_output_tags(
    paths: Sequence[Path],
    progress_callback: ProgressCallback | None = None,
) -> dict[Path, dict[str, object]]:
    """Read cleanup tags from output files in bounded ExifTool batches."""
    unique_paths = tuple(dict.fromkeys(path.resolve(strict=False) for path in paths))
    config_path = Path(__file__).resolve().parents[1] / "conf" / "exiftool.conf"
    result: dict[Path, dict[str, object]] = {}
    for start in range(0, len(unique_paths), _TAG_BATCH_SIZE):
        batch = unique_paths[start : start + _TAG_BATCH_SIZE]
        command = ["exiftool"]
        if config_path.is_file():
            command.extend(["-config", str(config_path)])
        command.extend(
            [
                "-json",
                "-VBCEncoder",
                "-VBCSourceParts",
                *(str(path) for path in batch),
            ]
        )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise SourceCleanupError("ExifTool is not installed") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            raise SourceCleanupError(f"ExifTool tag scan failed: {detail}")
        try:
            entries = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SourceCleanupError("ExifTool returned invalid JSON") from exc
        for entry in entries:
            source_value = entry.get("SourceFile")
            if source_value:
                result[Path(str(source_value)).resolve(strict=False)] = entry
        if progress_callback is not None:
            progress_callback(
                "Reading VBC tags",
                min(start + len(batch), len(unique_paths)),
                len(unique_paths),
            )
    return result


def _parse_source_parts(value: object) -> set[int] | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return {value} if value > 0 else None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not re.fullmatch(r"\d+(?:\s*,\s*\d+)*", normalized):
        return None
    parts = {int(item.strip()) for item in normalized.split(",")}
    if 0 in parts:
        return None
    return parts


def _probe_missing_group(
    group: SourceGroup,
    ffprobe_adapter: FFprobeAdapter,
) -> tuple[str, int]:
    has_usable_video = False
    has_probe_failure = False
    has_moov_failure = False
    usable_video_size_bytes = 0
    for _, source_path in group.sources:
        try:
            part_info = ffprobe_adapter.get_part_info(
                source_path,
                scan_packet_timeline=False,
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            if "moov atom not found" in str(exc).lower():
                has_moov_failure = True
            else:
                has_probe_failure = True
            continue
        if bool(part_info.get("has_video_stream")) and int(
            part_info.get("video_packets") or 0
        ) > 0:
            has_usable_video = True
            usable_video_size_bytes += source_path.stat().st_size
    if has_moov_failure:
        return "corrupt_moov", usable_video_size_bytes
    if has_probe_failure:
        return "probe_failed", usable_video_size_bytes
    if has_usable_video:
        return "has_video", usable_video_size_bytes
    return "no_video", 0


def _probe_source_has_usable_video(
    source_path: Path,
    ffprobe_adapter: FFprobeAdapter,
) -> bool | None:
    try:
        part_info = ffprobe_adapter.get_part_info(
            source_path,
            scan_packet_timeline=False,
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return None
    return bool(part_info.get("has_video_stream")) and int(
        part_info.get("video_packets") or 0
    ) > 0


def _related_metadata_paths(
    recording_id: str,
    metadata_search_dirs: Sequence[Path],
) -> tuple[Path, ...]:
    paths: list[Path] = []
    for directory in metadata_search_dirs:
        for suffix in (".json", ".err"):
            candidate = directory / f"ttracker-{recording_id}{suffix}"
            if candidate.is_file() and not candidate.is_symlink():
                paths.append(candidate)
    return tuple(dict.fromkeys(paths))


def _quarantine_status_from_markers(
    metadata_paths: Sequence[Path],
) -> tuple[str, str] | None:
    marker_texts: list[str] = []
    for path in metadata_paths:
        if path.suffix != ".err":
            continue
        try:
            marker_texts.append(path.read_text(errors="replace").lower())
        except OSError:
            continue
    combined = "\n".join(marker_texts)
    if "moov atom not found" in combined:
        return "CORRUPT_MOOV", "error marker: moov atom not found"
    if "ffmpeg exited with code -6" in combined:
        return "FFMPEG_SIGABRT", "error marker: FFmpeg terminated with SIGABRT"
    if "ffmpeg exited with code -11" in combined:
        return "FFMPEG_SIGSEGV", "error marker: FFmpeg terminated with SIGSEGV"
    if "hardware is lacking required capabilities" in combined:
        return "HARDWARE_UNSUPPORTED", "error marker: hardware capability unavailable"
    if (
        "invalid video dimensions" in combined
        or "invalid data found when processing input" in combined
    ):
        return "CORRUPT_INPUT", "error marker: permanently invalid video input"
    return None


def analyze_source_archive(
    source_root: Path,
    compressed_root: Path,
    *,
    verify_vbc_tags: bool = False,
    min_size_bytes: int | None = None,
    completed_recording_ids: set[str] | None = None,
    metadata_search_dirs: Sequence[Path] = (),
    quarantine_root: Path | None = None,
    progress_callback: ProgressCallback | None = None,
) -> CleanupResult:
    """Classify archived source files without consulting VBC manifests."""
    if min_size_bytes is not None and min_size_bytes < 0:
        raise SourceCleanupError("minimum source size cannot be negative")
    groups, symlinks_ignored, non_video_ignored = collect_source_groups(source_root)
    if compressed_root.is_symlink():
        raise SourceCleanupError(
            f"compressed output directory cannot be a symlink: {compressed_root}"
        )
    compressed_root = compressed_root.resolve(strict=True)
    if not compressed_root.is_dir():
        raise SourceCleanupError(f"not a directory: {compressed_root}")

    families: dict[tuple[Path, str], tuple[Path, ...]] = {}
    group_sizes: dict[tuple[Path, str], int] = {}
    all_outputs: list[Path] = []
    for index, group in enumerate(groups, start=1):
        group_key = (group.relative_dir, group.output_name)
        group_size = sum(path.stat().st_size for _, path in group.sources)
        group_sizes[group_key] = group_size
        base_output = compressed_root / group.relative_dir / group.output_name
        if min_size_bytes is not None and group_size < min_size_bytes:
            family = ()
        else:
            family = _output_family(base_output)
        families[group_key] = family
        all_outputs.extend(family)
        if progress_callback is not None:
            progress_callback("Locating outputs", index, len(groups))

    result = CleanupResult(
        symlinks_ignored=symlinks_ignored,
        non_video_ignored=non_video_ignored,
        min_size_bytes=min_size_bytes,
    )
    ffprobe_adapter = FFprobeAdapter()
    try:
        if progress_callback is None:
            tags = _read_output_tags(all_outputs)
        else:
            progress_callback("Reading VBC tags", 0, len(all_outputs))
            tags = _read_output_tags(all_outputs, progress_callback)
    except SourceCleanupError as exc:
        if verify_vbc_tags:
            raise
        tags = {}
        result.tag_scan_warning = f"{exc}; using filename-only legacy fallback"

    for index, group in enumerate(groups, start=1):
        if progress_callback is not None:
            progress_callback("Matching archived sources", index - 1, len(groups))
        base_output = compressed_root / group.relative_dir / group.output_name
        group_key = (group.relative_dir, group.output_name)
        group_size = group_sizes[group_key]
        if min_size_bytes is not None and group_size < min_size_bytes:
            for part_number, source_path in group.sources:
                result.decisions.append(
                    SourceDecision(
                        source_path,
                        base_output,
                        part_number,
                        "BELOW_MIN_SIZE",
                        f"group size {group_size} B is below {min_size_bytes} B",
                        size_bytes=source_path.stat().st_size,
                    )
                )
            continue

        family = families[group_key]
        if not family:
            related_metadata = _related_metadata_paths(
                base_output.stem,
                metadata_search_dirs,
            )
            marker_status = _quarantine_status_from_markers(related_metadata)
            if marker_status is not None:
                status, detail = marker_status
            else:
                probe_result, usable_video_size_bytes = _probe_missing_group(
                    group,
                    ffprobe_adapter,
                )
                if probe_result == "corrupt_moov":
                    status = "CORRUPT_MOOV"
                    detail = "ffprobe failed: moov atom not found"
                elif (
                    completed_recording_ids is not None
                    and base_output.stem in completed_recording_ids
                    and probe_result == "no_video"
                ):
                    status = "DONE_NO_VIDEO"
                    detail = "completed manifest; group has no usable video packets"
                elif (
                    completed_recording_ids is not None
                    and base_output.stem in completed_recording_ids
                    and probe_result == "has_video"
                    and min_size_bytes is not None
                    and usable_video_size_bytes < min_size_bytes
                ):
                    status = "DONE_BELOW_MIN_SIZE"
                    detail = (
                        "completed manifest; usable-video size "
                        f"{usable_video_size_bytes} B is below {min_size_bytes} B"
                    )
                else:
                    status = "OUTPUT_MISSING"
                    detail = "base output does not exist"
            for source_index, (part_number, source_path) in enumerate(group.sources):
                quarantine_path = None
                if status in _QUARANTINE_STATUSES and quarantine_root is not None:
                    quarantine_path = (
                        quarantine_root / group.relative_dir / source_path.name
                    )
                result.decisions.append(
                    SourceDecision(
                        source_path,
                        base_output,
                        part_number,
                        status,
                        detail,
                        size_bytes=source_path.stat().st_size,
                        quarantine_path=quarantine_path,
                        related_metadata_paths=(
                            related_metadata if source_index == 0 else ()
                        ),
                    )
                )
            continue

        tagged_outputs: dict[Path, set[int]] = {}
        invalid_tag = False
        for output_path in family:
            entry = tags.get(output_path.resolve(strict=False), {})
            raw_parts = _tag_value(entry, "VBCSourceParts")
            if raw_parts in (None, ""):
                continue
            parsed_parts = _parse_source_parts(raw_parts)
            if parsed_parts is None:
                invalid_tag = True
                break
            if verify_vbc_tags and not _has_tag(entry, "VBCEncoder"):
                invalid_tag = True
                break
            tagged_outputs[output_path] = parsed_parts

        if invalid_tag:
            for part_number, source_path in group.sources:
                result.decisions.append(
                    SourceDecision(
                        source_path,
                        base_output,
                        part_number,
                        "INVALID_TAG",
                        "VBCSourceParts is invalid or required VBCEncoder is missing",
                        size_bytes=source_path.stat().st_size,
                    )
                )
            continue

        if tagged_outputs:
            evidence_by_part: dict[int, list[Path]] = defaultdict(list)
            for output_path, part_numbers in tagged_outputs.items():
                for part_number in part_numbers:
                    evidence_by_part[part_number].append(output_path)
            for part_number, source_path in group.sources:
                evidence = tuple(evidence_by_part.get(part_number, ()))
                if evidence:
                    result.decisions.append(
                        SourceDecision(
                            source_path,
                            base_output,
                            part_number,
                            "VERIFIED",
                            "part listed by VBCSourceParts",
                            evidence,
                            source_path.stat().st_size,
                        )
                    )
                else:
                    has_usable_video = _probe_source_has_usable_video(
                        source_path,
                        ffprobe_adapter,
                    )
                    if has_usable_video is False:
                        result.decisions.append(
                            SourceDecision(
                                source_path,
                                base_output,
                                part_number,
                                "IGNORED_NO_VIDEO",
                                "part omitted by VBC and has no usable video packets",
                                size_bytes=source_path.stat().st_size,
                            )
                        )
                        continue
                    result.decisions.append(
                        SourceDecision(
                            source_path,
                            base_output,
                            part_number,
                            "UNMAPPED_SOURCE",
                            "part is not listed by any output",
                            size_bytes=source_path.stat().st_size,
                        )
                    )
            continue

        base_tags = tags.get(base_output.resolve(strict=False), {})
        if verify_vbc_tags and not _has_tag(base_tags, "VBCEncoder"):
            status = "UNVERIFIED_OUTPUT"
            detail = "legacy output has no VBCEncoder tag"
            evidence: tuple[Path, ...] = ()
        else:
            status = "LEGACY_MATCH"
            detail = "output exists (legacy filename match)"
            evidence = (base_output,)
        for part_number, source_path in group.sources:
            result.decisions.append(
                SourceDecision(
                    source_path,
                    base_output,
                    part_number,
                    status,
                    detail,
                    evidence,
                    source_path.stat().st_size,
                )
            )
    if progress_callback is not None:
        progress_callback("Matching archived sources", len(groups), len(groups))
    return result


def _quarantine_corrupt_source(decision: SourceDecision) -> bool:
    destination = decision.quarantine_path
    if destination is None:
        return False
    source = decision.source_path
    if source.is_symlink() or not source.is_file() or destination.exists():
        return False

    moves: list[tuple[Path, Path]] = [(source, destination)]
    for metadata_path in decision.related_metadata_paths:
        if metadata_path.is_symlink() or not metadata_path.is_file():
            continue
        metadata_destination = destination.parent / metadata_path.name
        if metadata_destination.exists():
            return False
        moves.append((metadata_path, metadata_destination))

    moved: list[tuple[Path, Path]] = []
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        for move_source, move_destination in moves:
            shutil.move(str(move_source), str(move_destination))
            if move_source.exists() or not move_destination.is_file():
                raise OSError(
                    f"move verification failed: {move_source} -> {move_destination}"
                )
            moved.append((move_source, move_destination))
    except OSError:
        for move_source, move_destination in reversed(moved):
            try:
                if move_destination.exists() and not move_source.exists():
                    move_source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(move_destination), str(move_source))
            except OSError:
                pass
        return False
    return True


def delete_verified_sources(
    result: CleanupResult,
    *,
    dry_run: bool,
    limit: int | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Delete output-verified sources and sources below the configured size floor."""
    result.delete_limit = limit
    eligible = [
        decision for decision in result.decisions if decision.deletion_eligible
    ]
    selected = eligible if limit is None else eligible[:limit]
    phase = "Previewing deletions" if dry_run else "Deleting sources"
    if progress_callback is not None:
        progress_callback(phase, 0, len(selected))

    for index, decision in enumerate(selected, start=1):
        try:
            if decision.verified and (
                not decision.evidence_outputs
                or any(
                    output.is_symlink() or not output.is_file()
                    for output in decision.evidence_outputs
                )
            ):
                result.failed += 1
                continue
            source = decision.source_path
            if source.is_symlink() or not source.is_file():
                result.failed += 1
                continue
            if decision.status in _QUARANTINE_STATUSES:
                if dry_run:
                    result.would_quarantine += 1
                    result.would_quarantine_paths.append(source)
                elif _quarantine_corrupt_source(decision):
                    result.quarantined += 1
                    result.quarantined_paths.append(decision.quarantine_path)
                else:
                    result.failed += 1
                continue
            if dry_run:
                result.would_delete += 1
                result.would_delete_paths.append(source)
                continue
            try:
                source.unlink()
            except OSError:
                result.failed += 1
            else:
                result.deleted += 1
                result.deleted_paths.append(source)
        finally:
            if progress_callback is not None:
                progress_callback(phase, index, len(selected))


def _render_result(result: CleanupResult, console: Console, *, show_all: bool) -> None:
    inventory = Table(title="Source archive inventory", box=None)
    inventory.add_column("Status", style="cyan")
    inventory.add_column("Files", justify="right", style="bold")
    counts = Counter(decision.status for decision in result.decisions)
    for status, count in sorted(counts.items()):
        inventory.add_row(status, str(count))
    inventory.add_row("SYMLINKS_IGNORED", str(result.symlinks_ignored))
    inventory.add_row("NON_VIDEO_IGNORED", str(result.non_video_ignored))
    console.print(inventory)

    visible_decisions = (
        result.decisions
        if show_all
        else [
            decision
            for decision in result.decisions
            if not decision.deletion_eligible
        ]
    )
    decisions = visible_decisions if show_all else visible_decisions[:100]
    table = Table(
        title=(
            "Source verification • all"
            if show_all
            else "Source verification • attention required"
        ),
        title_style="bold cyan",
        header_style="bold",
        border_style="bright_black",
    )
    table.add_column("Status", no_wrap=True)
    table.add_column("Source", style="bold", overflow="fold")
    table.add_column("Size", justify="right", no_wrap=True)
    table.add_column("Output / detail", overflow="fold")
    styles = {
        "VERIFIED": "bold green",
        "LEGACY_MATCH": "green",
        "BELOW_MIN_SIZE": "cyan",
        "DONE_BELOW_MIN_SIZE": "cyan",
        "DONE_NO_VIDEO": "cyan",
        "IGNORED_NO_VIDEO": "cyan",
        "CORRUPT_MOOV": "yellow",
        "CORRUPT_INPUT": "yellow",
        "FFMPEG_SIGABRT": "yellow",
        "FFMPEG_SIGSEGV": "yellow",
        "HARDWARE_UNSUPPORTED": "yellow",
        "UNMAPPED_SOURCE": "yellow",
        "OUTPUT_MISSING": "bold red",
        "INVALID_TAG": "bold red",
        "UNVERIFIED_OUTPUT": "yellow",
    }
    for decision in decisions:
        table.add_row(
            Text(decision.status, style=styles.get(decision.status, "white")),
            str(decision.source_path),
            _format_size(decision.size_bytes),
            f"{decision.output_path} • {decision.detail}",
        )
    if len(decisions) < len(visible_decisions):
        table.add_row(
            "…", f"{len(visible_decisions) - len(decisions)} more", "", ""
        )
    if decisions:
        console.print(table)
    if result.tag_scan_warning:
        console.print(f"[bold yellow]Warning:[/] {result.tag_scan_warning}")
    if result.min_size_bytes is not None:
        console.print(
            "[bold cyan]Size policy[/] • sources in a logical group below "
            f"{_format_size(result.min_size_bytes)} are deletion-eligible without an output"
        )
    if result.delete_limit is not None:
        actions = Table(
            title=f"Deletion actions • limit {result.delete_limit}",
            title_style="bold cyan",
            header_style="bold",
            border_style="bright_black",
        )
        actions.add_column("Action", no_wrap=True)
        actions.add_column("Source", style="bold", overflow="fold")
        for path in result.deleted_paths:
            actions.add_row(Text("DELETED", style="bold green"), str(path))
        for path in result.would_delete_paths:
            actions.add_row(Text("WOULD_DELETE", style="bold yellow"), str(path))
        for path in result.quarantined_paths:
            actions.add_row(Text("QUARANTINED", style="bold cyan"), str(path))
        for path in result.would_quarantine_paths:
            actions.add_row(Text("WOULD_QUARANTINE", style="bold yellow"), str(path))
        console.print(actions)
    console.print(
        "[bold cyan]Cleanup summary[/] • "
        f"deleted={result.deleted} • would_delete={result.would_delete} • "
        f"quarantined={result.quarantined} • "
        f"would_quarantine={result.would_quarantine} • failed={result.failed}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a VBC source archive against compressed outputs. Completed "
            "manifest names are used only to recognize no-video groups. Plain "
            "invocation is read-only."
        )
    )
    parser.add_argument(
        "source_archive",
        type=Path,
        nargs="?",
        help="defaults to metadata.move_after_success_dir from the VBC config",
    )
    parser.add_argument(
        "compressed_dir",
        type=Path,
        nargs="?",
        help="defaults to the unambiguous output root in current manifests",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("conf/vbc.yaml"),
        help="VBC configuration path used for omitted values",
    )
    parser.add_argument(
        "--verify-vbc-tags",
        action="store_true",
        help="require VBCEncoder for both tagged and legacy output matches",
    )
    parser.add_argument(
        "--min-size-bytes",
        type=int,
        default=None,
        help=(
            "treat logical source groups below this size as deletion-eligible "
            "without an output (default: general.min_size_bytes from config)"
        ),
    )
    parser.add_argument(
        "--delete-verified",
        action="store_true",
        help=(
            "delete verified/ignored sources and quarantine sources with "
            "recognized terminal errors"
        ),
    )
    parser.add_argument(
        "--delete-limit",
        "--limit-delete",
        type=int,
        default=None,
        metavar="N",
        help="delete at most N eligible sources; requires --delete-verified",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show how many deletion-eligible sources would be deleted",
    )
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="show every source, including verified and below-minimum entries",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.delete_limit is not None and args.delete_limit < 1:
        parser.error("--delete-limit must be at least 1")
    if args.delete_limit is not None and not args.delete_verified:
        parser.error("--delete-limit requires --delete-verified")
    console = Console()
    try:
        source_archive, compressed_dir, min_size_bytes, config = _resolve_cli_settings(
            args.source_archive,
            args.compressed_dir,
            args.min_size_bytes,
            args.config,
        )
        metadata_error_dirs = _metadata_error_dirs(config)
        quarantine_root = (
            metadata_error_dirs[0] if len(metadata_error_dirs) == 1 else None
        )
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=console,
        )
        with progress:
            task_id = progress.add_task("Indexing source archive", total=None)

            def update_progress(description: str, completed: int, total: int) -> None:
                progress.update(
                    task_id,
                    description=description,
                    completed=completed,
                    total=total,
                )

            result = analyze_source_archive(
                source_archive,
                compressed_dir,
                verify_vbc_tags=args.verify_vbc_tags,
                min_size_bytes=min_size_bytes,
                completed_recording_ids=_completed_recording_ids(config),
                metadata_search_dirs=_metadata_search_dirs(config),
                quarantine_root=quarantine_root,
                progress_callback=update_progress,
            )
        if args.delete_verified:
            delete_progress = Progress(
                SpinnerColumn(),
                TextColumn("[cyan]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeRemainingColumn(),
                console=console,
            )
            with delete_progress:
                delete_task_id = delete_progress.add_task(
                    "Preparing deletion", total=None
                )

                def update_delete_progress(
                    description: str, completed: int, total: int
                ) -> None:
                    delete_progress.update(
                        delete_task_id,
                        description=description,
                        completed=completed,
                        total=total,
                    )

                delete_verified_sources(
                    result,
                    dry_run=args.dry_run,
                    limit=args.delete_limit,
                    progress_callback=update_delete_progress,
                )
    except (OSError, SourceCleanupError) as exc:
        Console(stderr=True).print(f"[bold red]Error:[/] {exc}")
        return 1
    _render_result(result, console, show_all=args.show_all)
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
