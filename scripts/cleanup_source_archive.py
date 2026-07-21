#!/usr/bin/env python3
"""Verify archived sources against compressed outputs and delete explicit matches."""

from __future__ import annotations

import argparse
import json
import re
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

_PART_SUFFIX = re.compile(r"^(?P<base>.+)_part(?P<number>\d+)$", re.I)
_TAG_BATCH_SIZE = 200
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


class SourceCleanupError(RuntimeError):
    """Raised when source archive verification cannot proceed safely."""


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

    @property
    def verified(self) -> bool:
        return self.status in {"VERIFIED", "LEGACY_MATCH"}


@dataclass
class CleanupResult:
    decisions: list[SourceDecision] = field(default_factory=list)
    deleted: int = 0
    would_delete: int = 0
    failed: int = 0
    symlinks_ignored: int = 0
    non_video_ignored: int = 0
    tag_scan_warning: str | None = None


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
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not re.fullmatch(r"\d+(?:\s*,\s*\d+)*", normalized):
        return None
    parts = {int(item.strip()) for item in normalized.split(",")}
    if 0 in parts:
        return None
    return parts


def analyze_source_archive(
    source_root: Path,
    compressed_root: Path,
    *,
    verify_vbc_tags: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> CleanupResult:
    """Classify archived source files without consulting VBC manifests."""
    groups, symlinks_ignored, non_video_ignored = collect_source_groups(source_root)
    if compressed_root.is_symlink():
        raise SourceCleanupError(
            f"compressed output directory cannot be a symlink: {compressed_root}"
        )
    compressed_root = compressed_root.resolve(strict=True)
    if not compressed_root.is_dir():
        raise SourceCleanupError(f"not a directory: {compressed_root}")

    families: dict[tuple[Path, str], tuple[Path, ...]] = {}
    all_outputs: list[Path] = []
    for index, group in enumerate(groups, start=1):
        base_output = compressed_root / group.relative_dir / group.output_name
        family = _output_family(base_output)
        families[(group.relative_dir, group.output_name)] = family
        all_outputs.extend(family)
        if progress_callback is not None:
            progress_callback("Locating outputs", index, len(groups))

    result = CleanupResult(
        symlinks_ignored=symlinks_ignored,
        non_video_ignored=non_video_ignored,
    )
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
        family = families[(group.relative_dir, group.output_name)]
        if not family:
            for part_number, source_path in group.sources:
                result.decisions.append(
                    SourceDecision(
                        source_path,
                        base_output,
                        part_number,
                        "OUTPUT_MISSING",
                        "base output does not exist",
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
                        )
                    )
                else:
                    result.decisions.append(
                        SourceDecision(
                            source_path,
                            base_output,
                            part_number,
                            "UNMAPPED_SOURCE",
                            "part is not listed by any output",
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
            detail = "output exists; VBCSourceParts is unavailable"
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
                )
            )
    if progress_callback is not None:
        progress_callback("Matching archived sources", len(groups), len(groups))
    return result


def delete_verified_sources(
    result: CleanupResult,
    *,
    dry_run: bool,
) -> None:
    """Delete only sources with still-present output evidence."""
    for decision in result.decisions:
        if not decision.verified:
            continue
        if not decision.evidence_outputs or any(
            output.is_symlink() or not output.is_file()
            for output in decision.evidence_outputs
        ):
            result.failed += 1
            continue
        source = decision.source_path
        if source.is_symlink() or not source.is_file():
            result.failed += 1
            continue
        if dry_run:
            result.would_delete += 1
            continue
        try:
            source.unlink()
        except OSError:
            result.failed += 1
        else:
            result.deleted += 1


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

    decisions = result.decisions if show_all else result.decisions[:100]
    table = Table(
        title="Source verification",
        title_style="bold cyan",
        header_style="bold",
        border_style="bright_black",
    )
    table.add_column("Status", no_wrap=True)
    table.add_column("Source", style="bold", overflow="fold")
    table.add_column("Output / detail", overflow="fold")
    styles = {
        "VERIFIED": "bold green",
        "LEGACY_MATCH": "green",
        "UNMAPPED_SOURCE": "yellow",
        "OUTPUT_MISSING": "bold red",
        "INVALID_TAG": "bold red",
        "UNVERIFIED_OUTPUT": "yellow",
    }
    for decision in decisions:
        table.add_row(
            Text(decision.status, style=styles.get(decision.status, "white")),
            str(decision.source_path),
            f"{decision.output_path} • {decision.detail}",
        )
    if len(decisions) < len(result.decisions):
        table.add_row("…", f"{len(result.decisions) - len(decisions)} more", "")
    console.print(table)
    if result.tag_scan_warning:
        console.print(f"[bold yellow]Warning:[/] {result.tag_scan_warning}")
    console.print(
        "[bold cyan]Cleanup summary[/] • "
        f"deleted={result.deleted} • would_delete={result.would_delete} • "
        f"failed={result.failed}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a VBC source archive against compressed outputs without reading "
            "manifest JSON files. Plain invocation is read-only."
        )
    )
    parser.add_argument("source_archive", type=Path)
    parser.add_argument("compressed_dir", type=Path)
    parser.add_argument(
        "--verify-vbc-tags",
        action="store_true",
        help="require VBCEncoder for both tagged and legacy output matches",
    )
    parser.add_argument(
        "--delete-verified",
        action="store_true",
        help="delete only VERIFIED and LEGACY_MATCH source files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show how many verified sources would be deleted",
    )
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="show every source instead of the first 100 rows",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()
    try:
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
                args.source_archive,
                args.compressed_dir,
                verify_vbc_tags=args.verify_vbc_tags,
                progress_callback=update_progress,
            )
        if args.delete_verified:
            delete_verified_sources(result, dry_run=args.dry_run)
    except (OSError, SourceCleanupError) as exc:
        Console(stderr=True).print(f"[bold red]Error:[/] {exc}")
        return 1
    _render_result(result, console, show_all=args.show_all)
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
