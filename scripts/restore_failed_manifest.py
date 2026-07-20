#!/usr/bin/env python3
"""Restore one failed VBC manifest and its archived source files."""

from __future__ import annotations

import argparse
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from vbc.config.loader import load_config
from vbc.config.models import AppConfig, InputDirEntry
from vbc.domain.models import CompressionManifest


class RestoreError(RuntimeError):
    """Raised when a failed manifest cannot be restored safely."""


@dataclass(frozen=True)
class SourceRestore:
    archive_path: Path
    original_path: Path


@dataclass(frozen=True)
class RestoreResult:
    manifest_destination: Path
    restored_sources: tuple[SourceRestore, ...]
    already_present_sources: tuple[Path, ...]
    error_marker: Path | None
    dry_run: bool


def rollback_restored_sources(restored: Sequence[SourceRestore]) -> None:
    """Move sources restored for a retry back to their archive locations."""
    errors: list[str] = []
    for item in reversed(restored):
        try:
            if item.original_path.exists() and not item.archive_path.exists():
                item.archive_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item.original_path), str(item.archive_path))
        except OSError as exc:
            errors.append(str(exc))
    if errors:
        raise RestoreError(f"source rollback failed: {errors}")


def restore_failed_sources(result: RestoreResult) -> tuple[SourceRestore, ...]:
    """Restore only archived sources, leaving the failed JSON unpublished."""
    moved: list[SourceRestore] = []
    try:
        for item in result.restored_sources:
            item.original_path.parent.mkdir(parents=True, exist_ok=True)
            if item.original_path.exists():
                raise FileExistsError(
                    f"source destination appeared during restore: {item.original_path}"
                )
            shutil.move(str(item.archive_path), str(item.original_path))
            moved.append(item)
            if item.archive_path.exists() or not item.original_path.is_file():
                raise OSError(
                    "source move verification failed: "
                    f"{item.archive_path} -> {item.original_path}"
                )
    except (OSError, RestoreError) as exc:
        try:
            rollback_restored_sources(moved)
        except RestoreError as rollback_exc:
            raise RestoreError(
                f"source restore failed: {exc}; {rollback_exc}"
            ) from exc
        raise RestoreError(f"source restore failed: {exc}") from exc
    return tuple(moved)


def _deduplicated_enabled_entries(
    config: AppConfig,
) -> tuple[list[InputDirEntry], list[int]]:
    entries: list[InputDirEntry] = []
    original_indices: list[int] = []
    seen_paths: set[str] = set()
    for index, entry in enumerate(config.input_dirs):
        if not entry.enabled or entry.path in seen_paths:
            continue
        seen_paths.add(entry.path)
        entries.append(entry)
        original_indices.append(index)
    return entries, original_indices


def _aligned_error_dirs(
    config: AppConfig,
    entries: list[InputDirEntry],
    original_indices: list[int],
) -> list[Path]:
    if config.errors_dirs:
        raw_enabled_count = sum(entry.enabled for entry in config.input_dirs)
        if len(config.errors_dirs) == len(entries):
            return [Path(path) for path in config.errors_dirs]
        if len(config.errors_dirs) == raw_enabled_count:
            enabled_positions = {
                config_index: enabled_index
                for enabled_index, config_index in enumerate(
                    index
                    for index, entry in enumerate(config.input_dirs)
                    if entry.enabled
                )
            }
            return [
                Path(config.errors_dirs[enabled_positions[index]])
                for index in original_indices
            ]
        raise RestoreError(
            "errors_dirs count does not match enabled input_dirs in the configuration"
        )

    if config.suffix_errors_dirs is None:
        raise RestoreError(
            "configuration has neither errors_dirs nor suffix_errors_dirs"
        )
    return [
        Path(entry.path).with_name(
            f"{Path(entry.path).name}{config.suffix_errors_dirs}"
        )
        for entry in entries
    ]


def _resolve_metadata_destination(
    config: AppConfig,
    failed_manifest: Path,
) -> Path:
    entries, original_indices = _deduplicated_enabled_entries(config)
    error_dirs = _aligned_error_dirs(config, entries, original_indices)
    failed_parent = failed_manifest.parent.resolve(strict=False)
    matches = [
        Path(entry.path)
        for entry, error_dir in zip(entries, error_dirs, strict=True)
        if entry.metadata and error_dir.resolve(strict=False) == failed_parent
    ]
    if not matches:
        raise RestoreError(
            f"manifest is not inside a configured metadata error directory: {failed_parent}"
        )
    if len(matches) > 1:
        raise RestoreError(
            f"multiple metadata directories map to error directory: {failed_parent}"
        )
    return matches[0] / failed_manifest.name


def _validate_username(username: str) -> None:
    if username in ("", ".", "..") or Path(username).name != username:
        raise RestoreError(f"invalid producer username: {username!r}")


def _writable_parent(path: Path) -> Path:
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent


def restore_failed_manifest(
    failed_manifest: Path,
    config_path: Path,
    *,
    dry_run: bool = False,
) -> RestoreResult:
    """Restore archived inputs first and publish the failed manifest last."""
    if failed_manifest.is_symlink():
        raise RestoreError(f"failed manifest cannot be a symlink: {failed_manifest}")
    failed_manifest = failed_manifest.resolve(strict=True)
    if not failed_manifest.is_file() or failed_manifest.suffix.lower() != ".json":
        raise RestoreError(f"failed manifest must be a JSON file: {failed_manifest}")

    config = load_config(config_path)
    manifest_destination = _resolve_metadata_destination(config, failed_manifest)
    metadata_dir = manifest_destination.parent
    if not metadata_dir.is_dir():
        raise RestoreError(f"metadata directory does not exist: {metadata_dir}")
    if not os.access(metadata_dir, os.W_OK | os.X_OK):
        raise RestoreError(f"metadata directory is not writable: {metadata_dir}")
    if manifest_destination.exists():
        raise RestoreError(
            f"destination manifest already exists: {manifest_destination}"
        )

    try:
        manifest = CompressionManifest.model_validate_json(
            failed_manifest.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise RestoreError(f"invalid compression manifest: {exc}") from exc

    username = manifest.producer.username
    _validate_username(username)
    archive_root_value = config.metadata.move_after_success_dir
    archive_root = (
        Path(archive_root_value).resolve(strict=False)
        if archive_root_value is not None
        else None
    )

    restore_plan: list[SourceRestore] = []
    already_present: list[Path] = []
    missing: list[tuple[Path, Path | None]] = []
    seen_archives: set[Path] = set()
    for input_value in manifest.inputs:
        original_path = Path(input_value)
        if original_path.parent.name != username:
            raise RestoreError(
                "manifest input is outside its producer directory: "
                f"{original_path}"
            )
        archive_path = (
            archive_root / username / original_path.name
            if archive_root is not None
            else None
        )

        if original_path.exists():
            if original_path.is_symlink() or not original_path.is_file():
                raise RestoreError(
                    f"existing source destination is not a regular file: {original_path}"
                )
            if archive_path is not None and archive_path.exists():
                raise RestoreError(
                    "source exists in both original and archive locations: "
                    f"{original_path} ; {archive_path}"
                )
            already_present.append(original_path)
            continue

        if archive_path is None or not archive_path.is_file():
            missing.append((original_path, archive_path))
            continue
        if archive_path.is_symlink():
            raise RestoreError(f"archived source cannot be a symlink: {archive_path}")
        if archive_path in seen_archives:
            raise RestoreError(f"duplicate archived source path: {archive_path}")
        seen_archives.add(archive_path)
        restore_plan.append(SourceRestore(archive_path, original_path))

    if missing:
        details = "\n".join(
            "  original={original}\n  archive={archive}".format(
                original=original,
                archive=archive if archive is not None else "not configured",
            )
            for original, archive in missing
        )
        raise RestoreError(f"missing source files:\n{details}")

    for item in restore_plan:
        writable_parent = _writable_parent(item.original_path)
        if not writable_parent.is_dir() or not os.access(
            writable_parent, os.W_OK | os.X_OK
        ):
            raise RestoreError(
                f"source destination is not writable: {item.original_path.parent}"
            )

    error_marker = failed_manifest.with_suffix(".err")
    result = RestoreResult(
        manifest_destination=manifest_destination,
        restored_sources=tuple(restore_plan),
        already_present_sources=tuple(already_present),
        error_marker=error_marker if error_marker.is_file() else None,
        dry_run=dry_run,
    )
    if dry_run:
        return result

    moved: tuple[SourceRestore, ...] = ()
    manifest_moved = False
    try:
        moved = restore_failed_sources(result)

        if manifest_destination.exists():
            raise FileExistsError(
                f"destination manifest appeared during restore: {manifest_destination}"
            )
        shutil.move(str(failed_manifest), str(manifest_destination))
        manifest_moved = True
        if failed_manifest.exists() or not manifest_destination.is_file():
            raise OSError(
                f"manifest move verification failed: "
                f"{failed_manifest} -> {manifest_destination}"
            )
    except (OSError, RestoreError) as exc:
        rollback_errors: list[str] = []
        if (
            manifest_moved
            and manifest_destination.exists()
            and not failed_manifest.exists()
        ):
            try:
                shutil.move(str(manifest_destination), str(failed_manifest))
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        try:
            rollback_restored_sources(moved)
        except RestoreError as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        detail = (
            f"; rollback errors: {rollback_errors}" if rollback_errors else ""
        )
        raise RestoreError(f"restore failed: {exc}{detail}") from exc

    return result


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
