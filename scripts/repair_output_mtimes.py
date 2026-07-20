#!/usr/bin/env python3
"""Restore completed output mtimes from manifests in a metadata output directory."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.table import Table

from vbc.domain.models import CompressionManifest
from vbc.pipeline.output_timestamps import apply_output_timestamps

_EXIFTOOL_BATCH_SIZE = 200


class OutputMtimeRepairError(RuntimeError):
    """Raised when the repair target cannot be inspected safely."""


@dataclass(frozen=True)
class ManifestOutputs:
    manifest_path: Path
    source_mtime_ns: int
    base_output: Path
    candidates: tuple[Path, ...]


@dataclass
class RepairResult:
    manifests_scanned: int = 0
    manifests_invalid: int = 0
    missing_base_outputs: int = 0
    untagged_outputs_ignored: int = 0
    conflicting_outputs: int = 0
    output_files_considered: int = 0
    output_files_updated: int = 0
    dry_run: bool = False
    issues: list[str] = field(default_factory=list)


def _numbered_output_paths(base_output: Path) -> tuple[Path, ...]:
    parent = base_output.parent
    if not parent.is_dir():
        return ()
    pattern = re.compile(
        rf"^{re.escape(base_output.stem)}_(\d+){re.escape(base_output.suffix)}$"
    )
    numbered: list[tuple[int, Path]] = []
    for path in parent.iterdir():
        match = pattern.fullmatch(path.name)
        if match is not None:
            numbered.append((int(match.group(1)), path))
    return tuple(path for _, path in sorted(numbered))


def _load_manifest_outputs(manifest_path: Path) -> ManifestOutputs:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise OutputMtimeRepairError(
            f"manifest is not a regular file: {manifest_path}"
        )
    try:
        manifest = CompressionManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise OutputMtimeRepairError(f"invalid manifest: {exc}") from exc

    base_output = Path(manifest.output_path)
    candidates: list[Path] = []
    if base_output.exists():
        candidates.append(base_output)
    candidates.extend(_numbered_output_paths(base_output))
    return ManifestOutputs(
        manifest_path=manifest_path,
        source_mtime_ns=manifest.producer.source_latest_mtime_ns,
        base_output=base_output,
        candidates=tuple(candidates),
    )


def _has_vbc_encoder_tag(entry: dict[str, object]) -> bool:
    for key, value in entry.items():
        normalized = re.sub(r"[^a-z0-9]", "", key.split(":")[-1].lower())
        if normalized == "vbcencoder" and value:
            return True
    return False


def _find_vbc_tagged_outputs(paths: Sequence[Path]) -> set[Path]:
    """Read VBC tags in bounded ExifTool batches without modifying media."""
    unique_paths = tuple(dict.fromkeys(path.resolve(strict=False) for path in paths))
    tagged: set[Path] = set()
    for start in range(0, len(unique_paths), _EXIFTOOL_BATCH_SIZE):
        batch = unique_paths[start : start + _EXIFTOOL_BATCH_SIZE]
        command = [
            "exiftool",
            "-fast2",
            "-json",
            "-VBCEncoder",
            *(str(path) for path in batch),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise OutputMtimeRepairError(
                "ExifTool is required to distinguish VBC outputs from backups"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            raise OutputMtimeRepairError(
                f"read-only ExifTool tag scan failed: {detail}"
            )
        try:
            entries = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise OutputMtimeRepairError(
                "ExifTool returned invalid JSON during tag scan"
            ) from exc
        for entry in entries:
            source_value = entry.get("SourceFile")
            if source_value and _has_vbc_encoder_tag(entry):
                tagged.add(Path(str(source_value)).resolve(strict=False))
    return tagged


def repair_output_mtimes(
    metadata_out: Path,
    *,
    dry_run: bool = False,
) -> RepairResult:
    """Repair tagged outputs referenced by completed manifests."""
    if metadata_out.is_symlink():
        raise OutputMtimeRepairError(
            f"metadata output directory cannot be a symlink: {metadata_out}"
        )
    metadata_out = metadata_out.resolve(strict=True)
    if not metadata_out.is_dir():
        raise OutputMtimeRepairError(f"not a directory: {metadata_out}")

    result = RepairResult(dry_run=dry_run)
    manifests: list[ManifestOutputs] = []
    for manifest_path in sorted(metadata_out.glob("*.json")):
        result.manifests_scanned += 1
        try:
            manifests.append(_load_manifest_outputs(manifest_path))
        except (OSError, OutputMtimeRepairError) as exc:
            result.manifests_invalid += 1
            result.issues.append(f"{manifest_path.name}: {exc}")

    all_candidates = [path for item in manifests for path in item.candidates]
    tagged_outputs = _find_vbc_tagged_outputs(all_candidates)
    assignments: dict[Path, int] = {}
    conflicts: set[Path] = set()

    for item in manifests:
        base_output = item.base_output.resolve(strict=False)
        if base_output not in {path.resolve(strict=False) for path in item.candidates}:
            result.missing_base_outputs += 1
            result.issues.append(
                f"{item.manifest_path.name}: missing output {item.base_output}"
            )

        for candidate in item.candidates:
            resolved = candidate.resolve(strict=False)
            if candidate.is_symlink() or not candidate.is_file():
                result.issues.append(
                    f"{item.manifest_path.name}: unsafe output ignored {candidate}"
                )
                continue
            if resolved not in tagged_outputs:
                result.untagged_outputs_ignored += 1
                if resolved == base_output:
                    result.issues.append(
                        f"{item.manifest_path.name}: untagged base output ignored "
                        f"{candidate}"
                    )
                continue
            previous = assignments.get(resolved)
            if previous is not None and previous != item.source_mtime_ns:
                conflicts.add(resolved)
                result.issues.append(
                    f"conflicting timestamps for output {resolved}: "
                    f"{previous} and {item.source_mtime_ns}"
                )
                continue
            assignments[resolved] = item.source_mtime_ns

    for path in conflicts:
        assignments.pop(path, None)
    result.conflicting_outputs = len(conflicts)
    result.output_files_considered = len(assignments)

    grouped: dict[int, list[Path]] = defaultdict(list)
    for path, timestamp_ns in assignments.items():
        grouped[timestamp_ns].append(path)

    for timestamp_ns in sorted(grouped, reverse=True):
        paths = grouped[timestamp_ns]
        if dry_run:
            result.output_files_updated += sum(
                path.stat().st_mtime_ns != timestamp_ns for path in paths
            )
            continue
        update = apply_output_timestamps(paths, timestamp_ns)
        result.output_files_updated += update.files

    return result


def _render_result(console: Console, result: RepairResult) -> None:
    title = "Output timestamp dry run" if result.dry_run else "Output timestamps repaired"
    table = Table(title=title, show_header=False, box=None)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right", style="bold")
    table.add_row("Manifests scanned", str(result.manifests_scanned))
    table.add_row("Tagged outputs", str(result.output_files_considered))
    table.add_row(
        "Files to update" if result.dry_run else "Files updated",
        str(result.output_files_updated),
    )
    table.add_row("Missing base outputs", str(result.missing_base_outputs))
    table.add_row("Untagged files ignored", str(result.untagged_outputs_ignored))
    table.add_row("Invalid manifests", str(result.manifests_invalid))
    table.add_row("Conflicting outputs", str(result.conflicting_outputs))
    console.print(table)

    if result.issues:
        issues = Table(title="Issues", box=None)
        issues.add_column("#", justify="right", style="yellow")
        issues.add_column("Detail", style="red")
        for index, issue in enumerate(result.issues[:50], start=1):
            issues.add_row(str(index), issue)
        if len(result.issues) > 50:
            issues.add_row("…", f"{len(result.issues) - 50} more")
        console.print(issues)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Restore output file mtimes from completed VBC manifests."
        )
    )
    parser.add_argument("metadata_out", type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show changes without modifying filesystem timestamps",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()
    try:
        with console.status("[cyan]Scanning completed manifests and VBC outputs…"):
            result = repair_output_mtimes(args.metadata_out, dry_run=args.dry_run)
    except (OSError, OutputMtimeRepairError) as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        return 1
    _render_result(console, result)
    return 1 if result.manifests_invalid or result.conflicting_outputs else 0


if __name__ == "__main__":
    raise SystemExit(main())
