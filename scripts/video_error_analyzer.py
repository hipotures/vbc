#!/usr/bin/env python3
"""Analyze a VBC metadata error directory and run explicitly selected actions."""

from __future__ import annotations

import argparse
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from scripts.repair_failed_manifests import (
    MAX_ERROR_MARKER_BYTES,
    RepairError,
    collect_candidates,
    repair_candidate,
)


class AnalyzerError(RuntimeError):
    """Raised when the error directory cannot be analyzed safely."""


@dataclass(frozen=True)
class ErrorCategory:
    key: str
    label: str
    matches: Callable[[str], bool]
    delete_option: str | None = None


@dataclass(frozen=True)
class ErrorEntry:
    error_path: Path
    manifest_path: Path
    error_text: str
    category: ErrorCategory
    safe: bool = True


@dataclass(frozen=True)
class AnalysisOutcome:
    entry: ErrorEntry
    status: str
    detail: str


def _contains(pattern: str) -> Callable[[str], bool]:
    regex = re.compile(pattern, re.I)
    return lambda text: bool(regex.search(text))


ORPHAN = ErrorCategory(
    "orphan",
    "orphan marker (JSON missing)",
    lambda _text: False,
    "delete_orphans",
)
UNSAFE = ErrorCategory("unsafe", "unsafe entry", lambda _text: False)
FFMPEG_244 = ErrorCategory(
    "ffmpeg-244",
    "FFmpeg 244/-12 (repairable)",
    _contains(
        r"ffmpeg(?: concat)? exited with code 244\b"
        r"|(?:error(?: number)?|errno)\s*[:=]?\s*-12\b"
    ),
)
MOOV_MISSING = ErrorCategory(
    "moov-missing",
    "moov atom missing",
    _contains(r"moov atom not found"),
    "delete_moov_missing",
)
MISSING_INPUT = ErrorCategory(
    "missing-input",
    "manifest input missing",
    _contains(r"missing manifest input"),
    "delete_missing_input",
)
INVALID_DIMENSIONS = ErrorCategory(
    "invalid-dimensions",
    "invalid video dimensions",
    _contains(r"invalid video dimensions|dimensions[^\n]*\b0x0\b"),
    "delete_invalid_dimensions",
)
NO_VIDEO = ErrorCategory(
    "no-video",
    "no usable video",
    _contains(r"input has no video packets|manifest has no usable video groups"),
    "delete_no_video",
)
INVALID_BITSTREAM = ErrorCategory(
    "invalid-bitstream",
    "invalid video bitstream",
    _contains(
        r"missing picture in access unit|no frame!|no start code is found"
        r"|invalid data found when processing input"
    ),
    "delete_invalid_bitstream",
)
HARDWARE_CAPABILITY = ErrorCategory(
    "hardware-capability",
    "hardware capability failure",
    _contains(r"hardware is lacking required capabilities"),
    "delete_hardware_capability",
)
FFMPEG_ABORT = ErrorCategory(
    "ffmpeg-abort",
    "FFmpeg aborted (-6)",
    _contains(r"ffmpeg(?: concat)? exited with code -6\b"),
    "delete_ffmpeg_abort",
)
FFMPEG_SEGFAULT = ErrorCategory(
    "ffmpeg-segfault",
    "FFmpeg segmentation fault (-11)",
    _contains(r"ffmpeg(?: concat)? exited with code -11\b"),
    "delete_ffmpeg_segfault",
)
FFMPEG_234 = ErrorCategory(
    "ffmpeg-234",
    "FFmpeg exit code 234",
    _contains(r"ffmpeg(?: concat)? exited with code 234\b"),
    "delete_ffmpeg_234",
)
UNKNOWN = ErrorCategory(
    "unknown",
    "unknown error",
    lambda _text: True,
    "delete_unknown",
)

ERROR_CATEGORIES = (
    FFMPEG_244,
    MOOV_MISSING,
    MISSING_INPUT,
    INVALID_DIMENSIONS,
    NO_VIDEO,
    INVALID_BITSTREAM,
    HARDWARE_CAPABILITY,
    FFMPEG_ABORT,
    FFMPEG_SEGFAULT,
    FFMPEG_234,
    UNKNOWN,
)


def classify_error(error_text: str) -> ErrorCategory:
    """Return the first matching error category."""
    return next(
        category for category in ERROR_CATEGORIES if category.matches(error_text)
    )


def _read_marker(error_path: Path) -> str:
    if error_path.is_symlink() or not error_path.is_file():
        raise AnalyzerError(f"marker is not a regular file: {error_path}")
    size = error_path.stat().st_size
    if size > MAX_ERROR_MARKER_BYTES:
        raise AnalyzerError(
            f"marker exceeds {MAX_ERROR_MARKER_BYTES} bytes: {error_path}"
        )
    return error_path.read_text(encoding="utf-8", errors="replace")


def collect_error_entries(error_dir: Path) -> list[ErrorEntry]:
    """Collect and classify direct .err children without requiring JSON files."""
    if error_dir.is_symlink():
        raise AnalyzerError(f"error directory cannot be a symlink: {error_dir}")
    error_dir = error_dir.resolve(strict=True)
    if not error_dir.is_dir():
        raise AnalyzerError(f"not a directory: {error_dir}")

    entries: list[ErrorEntry] = []
    for error_path in sorted(error_dir.glob("*.err")):
        manifest_path = error_path.with_suffix(".json")
        try:
            error_text = _read_marker(error_path)
        except (OSError, AnalyzerError) as exc:
            entries.append(
                ErrorEntry(error_path, manifest_path, str(exc), UNSAFE, safe=False)
            )
            continue

        if manifest_path.is_symlink() or (
            manifest_path.exists() and not manifest_path.is_file()
        ):
            entries.append(
                ErrorEntry(
                    error_path,
                    manifest_path,
                    error_text,
                    UNSAFE,
                    safe=False,
                )
            )
        elif not manifest_path.exists():
            entries.append(ErrorEntry(error_path, manifest_path, error_text, ORPHAN))
        else:
            entries.append(
                ErrorEntry(
                    error_path,
                    manifest_path,
                    error_text,
                    classify_error(error_text),
                )
            )
    return entries


def _delete_metadata(entry: ErrorEntry, *, dry_run: bool) -> AnalysisOutcome:
    if not entry.safe:
        return AnalysisOutcome(entry, "FAILED", "unsafe filesystem entry")
    if entry.error_path.is_symlink() or not entry.error_path.is_file():
        return AnalysisOutcome(entry, "FAILED", "error marker changed after scan")

    manifest_exists = entry.manifest_path.exists()
    if entry.category is ORPHAN:
        if manifest_exists or entry.manifest_path.is_symlink():
            return AnalysisOutcome(entry, "FAILED", "JSON appeared after scan")
    elif entry.manifest_path.is_symlink() or not entry.manifest_path.is_file():
        return AnalysisOutcome(entry, "FAILED", "manifest changed after scan")

    removed = [entry.error_path.name]
    if manifest_exists:
        removed.insert(0, entry.manifest_path.name)
    if dry_run:
        return AnalysisOutcome(entry, "WOULD DELETE", ", ".join(removed))

    try:
        if manifest_exists:
            entry.manifest_path.unlink()
        entry.error_path.unlink()
    except OSError as exc:
        return AnalysisOutcome(entry, "FAILED", str(exc))
    return AnalysisOutcome(entry, "DELETED", ", ".join(removed))


def analyze_entries(
    entries: Sequence[ErrorEntry],
    args: argparse.Namespace,
    console: Console,
) -> list[AnalysisOutcome]:
    """Apply only explicitly selected repair or deletion actions."""
    action_selected = (
        args.repair_ffmpeg_244
        or args.delete_orphans
        or any(
            getattr(args, category.delete_option)
            for category in ERROR_CATEGORIES
            if category.delete_option is not None
        )
    )
    outcomes: list[AnalysisOutcome] = []

    for entry in entries:
        if entry.category is FFMPEG_244 and args.repair_ffmpeg_244:
            try:
                candidate = collect_candidates(entry.error_path)[0]
                repaired = repair_candidate(
                    candidate,
                    args.config,
                    console,
                    dry_run=args.dry_run,
                )
                outcomes.append(
                    AnalysisOutcome(entry, repaired.status, repaired.detail)
                )
            except (OSError, RepairError) as exc:
                outcomes.append(AnalysisOutcome(entry, "FAILED", str(exc)))
            continue

        delete_option = entry.category.delete_option
        if delete_option is not None and getattr(args, delete_option):
            outcomes.append(_delete_metadata(entry, dry_run=args.dry_run))
            continue

        first_line = entry.error_text.strip().splitlines()[0] or "empty error marker"
        outcomes.append(
            AnalysisOutcome(
                entry,
                "KEPT" if action_selected else "ANALYZED",
                first_line,
            )
        )
    return outcomes


def _render_results(
    entries: Sequence[ErrorEntry],
    outcomes: Sequence[AnalysisOutcome],
    console: Console,
) -> None:
    inventory = Table(title="Error inventory", box=None)
    inventory.add_column("Category", style="cyan")
    inventory.add_column("Count", justify="right", style="bold")
    counts = Counter(entry.category.label for entry in entries)
    for label, count in sorted(counts.items()):
        inventory.add_row(label, str(count))
    console.print(inventory)

    table = Table(
        title="Video error analysis",
        title_style="bold cyan",
        header_style="bold",
        border_style="bright_black",
        show_lines=False,
    )
    table.add_column("Status", no_wrap=True)
    table.add_column("Marker", style="bold", no_wrap=True)
    table.add_column("Classification / detail", overflow="fold")
    styles = {
        "ANALYZED": "bold cyan",
        "KEPT": "yellow",
        "READY": "bold cyan",
        "REPAIRED": "bold green",
        "WOULD DELETE": "bold magenta",
        "DELETED": "bold red",
        "SKIPPED": "yellow",
        "FAILED": "bold red",
    }
    for outcome in outcomes:
        detail = f"{outcome.entry.category.label} • {outcome.detail}"
        table.add_row(
            Text(outcome.status, style=styles.get(outcome.status, "white")),
            outcome.entry.error_path.name,
            detail,
        )
    console.print(table)

    status_counts = Counter(outcome.status for outcome in outcomes)
    summary = " • ".join(
        f"{status.lower().replace(' ', '_')}={count}"
        for status, count in sorted(status_counts.items())
    )
    console.print(f"[bold cyan]Analysis summary[/] • {summary}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze direct .err children of a VBC metadata error directory. "
            "No files are changed unless an explicit repair or delete flag is used."
        )
    )
    parser.add_argument("error_dir", type=Path, help="VBC metadata error directory")
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=Path("conf/vbc.yaml"),
        help="VBC configuration used only for repairs (default: conf/vbc.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show selected repairs and deletions without changing files",
    )
    repair = parser.add_argument_group("repair actions")
    repair.add_argument(
        "--repair-ffmpeg-244",
        action="store_true",
        help="repair FFmpeg 244/-12 tasks by recompressing without audio",
    )
    deletion = parser.add_argument_group("metadata deletion filters")
    deletion.add_argument(
        "--delete-orphans",
        action="store_true",
        help="delete .err markers that have no matching JSON",
    )
    deletion.add_argument(
        "--delete-moov-missing",
        action="store_true",
        help="delete JSON/.err pairs reporting a missing moov atom",
    )
    deletion.add_argument(
        "--delete-missing-input",
        action="store_true",
        help="delete JSON/.err pairs reporting missing manifest inputs",
    )
    deletion.add_argument(
        "--delete-invalid-dimensions",
        action="store_true",
        help="delete JSON/.err pairs reporting invalid video dimensions",
    )
    deletion.add_argument(
        "--delete-no-video",
        action="store_true",
        help="delete JSON/.err pairs reporting no usable video",
    )
    deletion.add_argument(
        "--delete-invalid-bitstream",
        action="store_true",
        help="delete JSON/.err pairs reporting an invalid video bitstream",
    )
    deletion.add_argument(
        "--delete-hardware-capability",
        action="store_true",
        help="delete JSON/.err pairs reporting missing hardware capabilities",
    )
    deletion.add_argument(
        "--delete-ffmpeg-abort",
        action="store_true",
        help="delete JSON/.err pairs reporting FFmpeg exit code -6",
    )
    deletion.add_argument(
        "--delete-ffmpeg-segfault",
        action="store_true",
        help="delete JSON/.err pairs reporting FFmpeg exit code -11",
    )
    deletion.add_argument(
        "--delete-ffmpeg-234",
        action="store_true",
        help="delete JSON/.err pairs reporting FFmpeg exit code 234",
    )
    deletion.add_argument(
        "--delete-unknown",
        action="store_true",
        help="delete JSON/.err pairs with unrecognized marker text",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()
    try:
        entries = collect_error_entries(args.error_dir)
    except (OSError, AnalyzerError) as exc:
        Console(stderr=True).print(
            Text.assemble(("✗ Analysis failed: ", "bold red"), str(exc))
        )
        return 1

    if not entries:
        console.print("[yellow]No direct .err files found.[/]")
        return 0

    console.print(
        f"[bold cyan]Video error analyzer[/] • {len(entries)} marker(s) • "
        "default: read-only"
    )
    try:
        outcomes = analyze_entries(entries, args, console)
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrupted.[/]")
        return 130
    _render_results(entries, outcomes, console)
    return 1 if any(outcome.status == "FAILED" for outcome in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
