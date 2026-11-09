#!/usr/bin/env python3
"""Terminal interface for failed-manifest repairs; the server core is headless."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from vbc.domain.events import JobCompleted, JobFailed, JobProgressUpdated, JobStarted
from vbc.infrastructure.event_bus import EventBus
from vbc.maintenance.repair import (
    RepairCandidate,
    RepairError,
    RepairOutcome,
    collect_candidates,
    repair_candidate,
)


class _RepairProgress:
    def __init__(self, console: Console, name: str):
        self.name = name
        self.progress = Progress(
            SpinnerColumn("arc", style="cyan"),
            TextColumn("{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        self.task_id = self.progress.add_task(
            f"[cyan]Preflight[/] {name}", total=100.0
        )

    def bind(self, event_bus: EventBus) -> None:
        event_bus.subscribe(
            JobStarted,
            lambda event: self.progress.update(
                self.task_id,
                description=f"[magenta]Compressing[/] {event.job.source_file.path.name}",
            ),
        )
        event_bus.subscribe(
            JobProgressUpdated,
            lambda event: self.progress.update(
                self.task_id,
                completed=max(0.0, min(100.0, event.progress_percent)),
            ),
        )
        event_bus.subscribe(
            JobCompleted,
            lambda _event: self.progress.update(self.task_id, completed=100.0),
        )
        event_bus.subscribe(
            JobFailed,
            lambda _event: self.progress.update(
                self.task_id, description=f"[red]Failed[/] {self.name}"
            ),
        )

    def __enter__(self) -> "_RepairProgress":
        self.progress.start()
        return self

    def __exit__(self, *_args) -> None:
        self.progress.stop()


def repair_with_progress(
    candidate: RepairCandidate,
    config_path: Path,
    console: Console,
    *,
    dry_run: bool = False,
) -> RepairOutcome:
    if dry_run:
        return repair_candidate(candidate, config_path, dry_run=True)
    event_bus = EventBus()
    progress = _RepairProgress(console, candidate.manifest_path.name)
    progress.bind(event_bus)
    with progress:
        return repair_candidate(candidate, config_path, event_bus=event_bus)


def _render_summary(
    outcomes: Sequence[RepairOutcome],
    console: Console,
    *,
    dry_run: bool,
) -> None:
    table = Table(
        title="Failed manifest repair",
        title_style="bold cyan",
        header_style="bold",
        border_style="bright_black",
        show_lines=False,
    )
    table.add_column("Status", no_wrap=True)
    table.add_column("Manifest", style="bold", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    styles = {
        "REPAIRED": "bold green",
        "READY": "bold cyan",
        "SKIPPED": "bold yellow",
        "FAILED": "bold red",
    }
    for outcome in outcomes:
        table.add_row(
            Text(outcome.status, style=styles.get(outcome.status, "white")),
            outcome.manifest_path.name,
            outcome.detail,
        )
    console.print(table)
    repaired = sum(outcome.status == "REPAIRED" for outcome in outcomes)
    ready = sum(outcome.status == "READY" for outcome in outcomes)
    skipped = sum(outcome.status == "SKIPPED" for outcome in outcomes)
    failed = sum(outcome.status == "FAILED" for outcome in outcomes)
    label = "Plan" if dry_run else "Repair"
    console.print(
        f"[bold cyan]{label} summary[/] • repaired={repaired} • ready={ready} "
        f"• skipped={skipped} • failed={failed} • FFmpeg RAM cap=50 GiB"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Repair one failed VBC manifest or scan a manifest error directory. "
            "Unsupported errors are reported and left unchanged."
        )
    )
    parser.add_argument("target", type=Path, help="Failed .json/.err file or directory")
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=Path("conf/vbc.yaml"),
        help="VBC configuration path (default: conf/vbc.yaml).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report supported repairs without changing files.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()
    try:
        candidates = collect_candidates(args.target)
    except (OSError, RepairError) as exc:
        Console(stderr=True).print(
            Text.assemble(("✗ Scan failed: ", "bold red"), str(exc))
        )
        return 1

    if not candidates:
        console.print("[yellow]No .err files found.[/]")
        return 0

    console.print(
        f"[bold cyan]Repair scan[/] • {len(candidates)} marker(s) • "
        "supported: FFmpeg 244/-12 • sequential execution • RAM cap: 50 GiB"
    )
    outcomes: list[RepairOutcome] = []
    try:
        for candidate in candidates:
            outcomes.append(
                repair_with_progress(
                    candidate,
                    args.config,
                    console,
                    dry_run=args.dry_run,
                )
            )
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrupted; current .tmp cleanup requested.[/]")
        return 130

    _render_summary(outcomes, console, dry_run=args.dry_run)
    return 1 if any(outcome.status == "FAILED" for outcome in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
