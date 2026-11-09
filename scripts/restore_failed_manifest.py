#!/usr/bin/env python3
"""Terminal interface for restoring failed manifests and archived sources."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console
from rich.table import Table
from rich.text import Text

from vbc.maintenance.restore import RestoreError, RestoreResult, restore_failed_manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Restore source files archived by VBC move_all and move one failed "
            "manifest back to its configured metadata directory."
        )
    )
    parser.add_argument("failed_manifest", type=Path)
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
        help="Validate and show the restore plan without moving files.",
    )
    return parser


def _render_result(result: RestoreResult, console: Console) -> None:
    table = Table(
        title="Failed manifest recovery",
        title_style="bold cyan",
        header_style="bold",
        border_style="bright_black",
        show_lines=False,
    )
    table.add_column("Status", no_wrap=True)
    table.add_column("Item", style="bold", no_wrap=True)
    table.add_column("Path", overflow="fold")

    source_action = "WOULD MOVE" if result.dry_run else "RESTORED"
    source_style = "bold magenta" if result.dry_run else "bold green"
    for item in result.restored_sources:
        path = Text(str(item.archive_path))
        path.append("  →  ", style="bright_black")
        path.append(str(item.original_path))
        table.add_row(Text(source_action, style=source_style), "Source", path)

    for path in result.already_present_sources:
        table.add_row(
            Text("PRESENT", style="bold cyan"),
            "Source",
            Text(str(path)),
        )

    manifest_action = "WOULD MOVE" if result.dry_run else "RESTORED"
    manifest_style = "bold magenta" if result.dry_run else "bold green"
    table.add_row(
        Text(manifest_action, style=manifest_style),
        "Manifest",
        Text(str(result.manifest_destination)),
    )
    if result.error_marker is not None:
        table.add_row(
            Text("RETAINED", style="bold yellow"),
            "Error marker",
            Text(str(result.error_marker)),
        )

    console.print(table)
    source_count = len(result.restored_sources) + len(result.already_present_sources)
    if result.dry_run:
        console.print(
            f"[bold magenta]○ Plan validated[/] • {source_count} source(s) ready "
            "• no files changed"
        )
    else:
        console.print(
            f"[bold green]✓ Recovery complete[/] • {source_count} source(s) ready "
            "• manifest published last"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()
    try:
        result = restore_failed_manifest(
            args.failed_manifest,
            args.config,
            dry_run=args.dry_run,
        )
    except (OSError, RestoreError, ValueError) as exc:
        Console(stderr=True).print(
            Text.assemble(("✗ Recovery failed: ", "bold red"), str(exc))
        )
        return 1

    _render_result(result, console)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
