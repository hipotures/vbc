#!/usr/bin/env python3
"""Generate VBC metadata manifests without modifying recording sources."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
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


PART_RE = re.compile(r"^(?P<base>.+)_part(?P<index>\d+)\.mp4$", re.IGNORECASE)
ROOT_RECORDING_RE = re.compile(r"^(?P<username>.+)_\d{8}_\d{6}$")
EXIFTOOL_FILE_FORMAT_ERROR_RE = re.compile(r"^Error: File format error - (.+)$")
EXIFTOOL_SUMMARY_RE = re.compile(
    r"^\d+ (?:directories scanned|image files read|files failed condition)$"
)
TAG_SCAN_BATCH_SIZE = 200
TagProgressCallback = Callable[[int, int], None]
IssueCallback = Callable[[str], None]


@dataclass(frozen=True)
class RecordingTask:
    username: str
    recording_id: str
    inputs: tuple[Path, ...]
    output_path: Path

    @property
    def multipart(self) -> bool:
        return len(self.inputs) > 1 or PART_RE.match(self.inputs[0].name) is not None


@dataclass
class GenerationResult:
    discovered: int = 0
    generated: int = 0
    existing_manifests: int = 0
    conflicting_manifests: int = 0
    existing_outputs: int = 0
    single_tasks: int = 0
    multipart_tasks: int = 0
    shadowed_singles: int = 0
    tagged_sources: int = 0
    recovered_legacy_first_parts: int = 0
    excluded_by_modified_before: int = 0
    issues: list[str] = field(default_factory=list)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _username_for(relative_path: Path, recording_id: str) -> str | None:
    if len(relative_path.parts) > 1:
        return relative_path.parts[0]
    match = ROOT_RECORDING_RE.match(recording_id)
    return match.group("username") if match else None


def _parse_modified_before(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be an ISO 8601 date-time, for example 2026-07-19T00:00:00+02:00"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include a timezone offset")
    return parsed


def _exiftool_batch_entries(
    paths: Sequence[Path],
    issue_callback: IssueCallback | None,
) -> list[dict[str, object]]:
    command = [
        "exiftool",
        "-fast2",
        "-json",
        "-VBCEncoder",
        *(str(path) for path in paths),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ExifTool is required to distinguish VBC outputs from legacy sources"
        ) from exc

    format_errors: list[str] = []
    unexpected_errors: list[str] = []
    for line in completed.stderr.splitlines():
        stripped = line.strip()
        if not stripped or EXIFTOOL_SUMMARY_RE.fullmatch(stripped):
            continue
        match = EXIFTOOL_FILE_FORMAT_ERROR_RE.fullmatch(stripped)
        if match is not None:
            format_errors.append(match.group(1))
        else:
            unexpected_errors.append(stripped)

    if completed.returncode != 0 and (
        unexpected_errors or not format_errors
    ):
        detail = "\n".join(unexpected_errors) or (
            completed.stderr.strip() or f"exit code {completed.returncode}"
        )
        raise RuntimeError(f"read-only ExifTool tag scan failed: {detail}")
    try:
        entries = json.loads(completed.stdout) if completed.stdout.strip() else []
    except json.JSONDecodeError as exc:
        raise RuntimeError("ExifTool returned invalid JSON during tag scan") from exc
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict) for entry in entries
    ):
        raise RuntimeError("ExifTool returned invalid JSON during tag scan")

    if issue_callback is not None:
        for path in format_errors:
            issue_callback(
                "ExifTool could not read tags; source kept for manifest "
                f"discovery: {path}"
            )
    return entries


def find_vbc_encoded_sources(
    recordings_dir: Path,
    *,
    progress_callback: TagProgressCallback | None = None,
    issue_callback: IssueCallback | None = None,
) -> set[Path]:
    """Find prior VBC outputs with bounded read-only ExifTool batches."""
    paths = tuple(
        path
        for path in sorted(recordings_dir.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() == ".mp4"
        and ".vbc-part" not in path.name.lower()
    )
    tagged: set[Path] = set()
    if progress_callback is not None:
        progress_callback(0, len(paths))
    for start in range(0, len(paths), TAG_SCAN_BATCH_SIZE):
        batch = paths[start : start + TAG_SCAN_BATCH_SIZE]
        entries = _exiftool_batch_entries(batch, issue_callback)
        for entry in entries:
            if entry.get("VBCEncoder") in (None, ""):
                continue
            source_value = entry.get("SourceFile")
            if not source_value:
                continue
            source_path = Path(str(source_value)).resolve(strict=False)
            if _is_within(source_path, recordings_dir):
                tagged.add(source_path)
        if progress_callback is not None:
            progress_callback(min(start + len(batch), len(paths)), len(paths))
    return tagged


def discover_tasks(
    recordings_dir: Path,
    compressed_dir: Path,
    vbc_encoded_sources: set[Path] | None = None,
) -> tuple[list[RecordingTask], GenerationResult]:
    """Discover legacy singles and ordered multipart groups using read-only operations."""
    result = GenerationResult()
    singles: dict[tuple[Path, str], Path] = {}
    part_groups: dict[tuple[Path, str], dict[int, Path]] = {}
    invalid_part_groups: set[tuple[Path, str]] = set()
    recovered_single_keys: set[tuple[Path, str]] = set()
    vbc_encoded_sources = vbc_encoded_sources or set()

    for path in sorted(recordings_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".mp4":
            continue
        relative_path = path.relative_to(recordings_dir)
        if path.is_symlink():
            result.issues.append(f"symlink ignored: {path}")
            continue
        if ".vbc-part" in path.name.lower():
            result.issues.append(f"VBC staging artifact ignored: {path}")
            continue
        if path.resolve(strict=False) in vbc_encoded_sources:
            result.tagged_sources += 1
            continue

        part_match = PART_RE.match(path.name)
        if part_match is None:
            singles[(relative_path.parent, path.stem)] = path
            continue

        recording_id = part_match.group("base")
        part_index = int(part_match.group("index"))
        key = (relative_path.parent, recording_id)
        indexed_parts = part_groups.setdefault(key, {})
        if part_index <= 0 or part_index in indexed_parts:
            invalid_part_groups.add(key)
            result.issues.append(
                f"duplicate or invalid part index {part_index}: {path}"
            )
            continue
        indexed_parts[part_index] = path

    tasks: list[RecordingTask] = []
    for key, indexed_parts in sorted(part_groups.items(), key=lambda item: item[0]):
        relative_parent, recording_id = key
        if key in invalid_part_groups:
            continue
        indices = sorted(indexed_parts)
        expected = list(range(1, indices[-1] + 1))
        inputs: tuple[Path, ...]
        if indices == expected:
            inputs = tuple(indexed_parts[index] for index in indices)
        elif indices == list(range(2, indices[-1] + 1)) and key in singles:
            inputs = (singles[key],) + tuple(indexed_parts[index] for index in indices)
            recovered_single_keys.add(key)
            result.recovered_legacy_first_parts += 1
        else:
            result.issues.append(
                f"multipart group has gaps and was ignored: {relative_parent / recording_id} "
                f"(found={indices}, expected={expected})"
            )
            invalid_part_groups.add(key)
            continue
        username = _username_for(relative_parent / f"{recording_id}.mp4", recording_id)
        if username is None:
            result.issues.append(
                f"cannot determine username; group ignored: {relative_parent / recording_id}"
            )
            invalid_part_groups.add(key)
            continue
        tasks.append(
            RecordingTask(
                username=username,
                recording_id=recording_id,
                inputs=inputs,
                output_path=compressed_dir / relative_parent / f"{recording_id}.mp4",
            )
        )

    for key, path in sorted(singles.items(), key=lambda item: item[0]):
        relative_parent, recording_id = key
        if key in recovered_single_keys:
            continue
        if key in part_groups:
            result.shadowed_singles += 1
            result.issues.append(f"single shadowed by multipart group: {path}")
            continue
        username = _username_for(path.relative_to(recordings_dir), recording_id)
        if username is None:
            result.issues.append(f"cannot determine username; file ignored: {path}")
            continue
        tasks.append(
            RecordingTask(
                username=username,
                recording_id=recording_id,
                inputs=(path,),
                output_path=compressed_dir / relative_parent / path.name,
            )
        )

    tasks.sort(key=lambda task: (task.output_path.as_posix(), task.recording_id))
    result.discovered = len(tasks)
    result.single_tasks = sum(not task.multipart for task in tasks)
    result.multipart_tasks = sum(task.multipart for task in tasks)
    result.existing_outputs = sum(task.output_path.is_file() for task in tasks)
    return tasks, result


def _manifest_payload(
    task: RecordingTask,
    source_policy: str,
    created_at: str,
) -> dict[str, object]:
    stats = [path.stat() for path in task.inputs]
    return {
        "schema_version": 1,
        "request_id": f"ttracker-{task.recording_id}",
        "created_at": created_at,
        "producer": {
            "app": "ttracker",
            "username": task.username,
            "recording_id": task.recording_id,
            "source_size_bytes": sum(stat.st_size for stat in stats),
            "source_latest_mtime_ns": max(stat.st_mtime_ns for stat in stats),
        },
        "operation": "concat_transcode",
        "inputs": [str(path) for path in task.inputs],
        "output_path": str(task.output_path),
        "source_policy": source_policy,
        "compression_profile": "tiktok",
        "error_policy": {"missing_input": "fail"},
    }


def _same_manifest_task(
    manifest_path: Path,
    payload: dict[str, object],
) -> bool:
    try:
        existing = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(existing, dict):
        return False
    existing = {key: value for key, value in existing.items() if key != "created_at"}
    planned = {key: value for key, value in payload.items() if key != "created_at"}
    return existing == planned


def generate_manifests(
    recordings_dir: Path,
    metadata_dir: Path,
    compressed_dir: Path,
    *,
    modified_before: datetime,
    source_policy: str = "keep",
    dry_run: bool = False,
    vbc_encoded_sources: set[Path] | None = None,
    tag_progress_callback: TagProgressCallback | None = None,
) -> GenerationResult:
    recordings_dir = recordings_dir.resolve(strict=True)
    if not recordings_dir.is_dir():
        raise ValueError(f"recordings path is not a directory: {recordings_dir}")
    if modified_before.tzinfo is None or modified_before.utcoffset() is None:
        raise ValueError("modified_before must include a timezone offset")
    metadata_dir = metadata_dir.resolve(strict=False)
    compressed_dir = compressed_dir.resolve(strict=False)
    if metadata_dir == recordings_dir or _is_within(metadata_dir, recordings_dir):
        raise ValueError(
            "metadata directory cannot be inside the read-only recordings tree"
        )
    if compressed_dir == recordings_dir or _is_within(compressed_dir, recordings_dir):
        raise ValueError("compressed directory cannot be inside the recordings tree")

    tag_scan_issues: list[str] = []
    if vbc_encoded_sources is None:
        vbc_encoded_sources = find_vbc_encoded_sources(
            recordings_dir,
            progress_callback=tag_progress_callback,
            issue_callback=tag_scan_issues.append,
        )
    else:
        vbc_encoded_sources = {
            path.resolve(strict=False) for path in vbc_encoded_sources
        }
    tasks, result = discover_tasks(
        recordings_dir,
        compressed_dir,
        vbc_encoded_sources,
    )
    result.issues[:0] = tag_scan_issues
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    cutoff_timestamp = modified_before.timestamp()
    manifest_names: set[str] = set()
    if not dry_run:
        metadata_dir.mkdir(parents=True, exist_ok=True)

    for task in tasks:
        manifest_name = f"ttracker-{task.recording_id}.json"
        if manifest_name in manifest_names:
            result.issues.append(
                f"duplicate manifest name ignored: {manifest_name} ({task.output_path})"
            )
            continue
        manifest_names.add(manifest_name)
        manifest_path = metadata_dir / manifest_name
        payload = _manifest_payload(task, source_policy, created_at)
        if manifest_path.exists():
            if _same_manifest_task(manifest_path, payload):
                result.existing_manifests += 1
            else:
                result.conflicting_manifests += 1
                result.issues.append(
                    "existing manifest differs and was not overwritten: "
                    f"{manifest_path}"
                )
            continue
        latest_mtime = max(path.stat().st_mtime for path in task.inputs)
        if latest_mtime >= cutoff_timestamp:
            result.excluded_by_modified_before += 1
            continue
        if sum(path.stat().st_size for path in task.inputs) <= 0:
            result.issues.append(f"zero-size task ignored: {task.output_path}")
            continue
        if dry_run:
            result.generated += 1
            continue
        try:
            with manifest_path.open("x", encoding="utf-8") as manifest_file:
                json.dump(payload, manifest_file, indent=2, ensure_ascii=False)
                manifest_file.write("\n")
        except FileExistsError:
            if _same_manifest_task(manifest_path, payload):
                result.existing_manifests += 1
            else:
                result.conflicting_manifests += 1
                result.issues.append(
                    "existing manifest differs and was not overwritten: "
                    f"{manifest_path}"
                )
            continue
        result.generated += 1

    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate VBC JSON manifests from legacy single and multipart MP4 files. "
            "The recordings tree is read-only."
        )
    )
    parser.add_argument("recordings_dir", type=Path)
    parser.add_argument("metadata_dir", type=Path)
    parser.add_argument(
        "--modified-before",
        type=_parse_modified_before,
        required=True,
        help=(
            "Generate a manifest only when every input is older than this timezone-aware "
            "ISO 8601 date-time."
        ),
    )
    parser.add_argument(
        "--compressed-dir",
        type=Path,
        help="Output root stored in manifests (default: sibling directory 'compressed').",
    )
    parser.add_argument(
        "--source-policy",
        choices=("keep", "delete_after_success", "move_after_success", "move_all"),
        default="keep",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report without creating the metadata directory or JSON files.",
    )
    return parser


def _render_summary(console: Console, result: GenerationResult) -> None:
    table = Table(title="Manifest generation summary", show_header=False, box=None)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right", style="bold")
    rows = (
        ("Discovered", result.discovered),
        ("Generated", result.generated),
        ("Single tasks", result.single_tasks),
        ("Multipart tasks", result.multipart_tasks),
        ("Existing outputs", result.existing_outputs),
        ("Existing manifests", result.existing_manifests),
        ("Conflicting manifests", result.conflicting_manifests),
        ("Shadowed singles", result.shadowed_singles),
        ("Tagged sources", result.tagged_sources),
        ("Recovered legacy first parts", result.recovered_legacy_first_parts),
        ("Excluded by modified-before", result.excluded_by_modified_before),
    )
    for label, value in rows:
        table.add_row(label, str(value))
    console.print(table)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    recordings_dir = args.recordings_dir.resolve(strict=True)
    compressed_dir = args.compressed_dir or recordings_dir.parent / "compressed"
    console = Console()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    with progress:
        task_id = progress.add_task("Reading VBC tags", total=None)

        def update_tag_progress(completed: int, total: int) -> None:
            progress.update(
                task_id,
                completed=completed,
                total=total,
            )

        result = generate_manifests(
            recordings_dir,
            args.metadata_dir,
            compressed_dir,
            modified_before=args.modified_before,
            source_policy=args.source_policy,
            dry_run=args.dry_run,
            tag_progress_callback=update_tag_progress,
        )
    _render_summary(console, result)
    for issue in result.issues:
        console.print(f"[yellow]warning:[/] {issue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
