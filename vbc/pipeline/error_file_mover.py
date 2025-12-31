import logging
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from rich.progress import Progress, BarColumn, SpinnerColumn, TextColumn, TimeRemainingColumn


def _normalize_extensions(extensions: List[str]) -> Set[str]:
    return {ext.lower().lstrip(".") for ext in extensions}


def _find_source_for_error(
    input_dir: Path,
    rel_err_path: Path,
    allowed_exts: Set[str],
) -> Optional[Path]:
    output_rel = rel_err_path.with_suffix("")
    direct = input_dir / output_rel
    if direct.exists():
        return direct

    base_parent = direct.parent
    if not base_parent.exists():
        return None

    candidates: List[Path] = []
    base_name = output_rel.name
    base_name_lower = base_name.lower()
    for entry in base_parent.iterdir():
        if not entry.is_file():
            continue
        if entry.stem != base_name and entry.stem.lower() != base_name_lower:
            continue
        ext = entry.suffix.lower().lstrip(".")
        if ext in allowed_exts:
            candidates.append(entry)

    if not candidates:
        return None
    candidates.sort(key=lambda path: path.name.lower())
    return candidates[0]


def collect_error_entries(
    input_dirs: Iterable[Path],
    output_dir_map: Dict[Path, Path],
    errors_dir_map: Dict[Path, Path],
) -> List[Tuple[Path, Path, Path, Path]]:
    error_entries: List[Tuple[Path, Path, Path, Path]] = []
    for input_dir in input_dirs:
        output_dir = output_dir_map.get(input_dir)
        errors_dir = errors_dir_map.get(input_dir)
        if output_dir is None or errors_dir is None:
            continue
        if not output_dir.exists():
            continue
        for err_file in sorted(output_dir.rglob("*.err")):
            error_entries.append((input_dir, output_dir, errors_dir, err_file))
    return error_entries


def move_failed_files(
    input_dirs: Iterable[Path],
    output_dir_map: Dict[Path, Path],
    errors_dir_map: Dict[Path, Path],
    extensions: List[str],
    logger: Optional[logging.Logger] = None,
    error_entries: Optional[List[Tuple[Path, Path, Path, Path]]] = None,
) -> int:
    allowed_exts = _normalize_extensions(extensions)
    error_entries = error_entries or collect_error_entries(
        input_dirs, output_dir_map, errors_dir_map
    )

    total = len(error_entries)
    if total == 0:
        if logger:
            logger.info("No .err files found for failed file relocation.")
        return 0

    if logger:
        logger.info(f"Relocating {total} failed files to errors directories.")

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
    ) as progress:
        task = progress.add_task("Moving failed files", total=total)
        for input_dir, output_dir, errors_dir, err_file in error_entries:
            rel_err = err_file.relative_to(output_dir)
            dest_err = errors_dir / rel_err
            dest_err.parent.mkdir(parents=True, exist_ok=True)
            if err_file.exists() and err_file != dest_err:
                shutil.move(str(err_file), str(dest_err))

            source_path = _find_source_for_error(input_dir, rel_err, allowed_exts)
            if source_path and source_path.exists():
                rel_source = source_path.relative_to(input_dir)
                dest_source = errors_dir / rel_source
                dest_source.parent.mkdir(parents=True, exist_ok=True)
                if source_path != dest_source:
                    shutil.move(str(source_path), str(dest_source))
            else:
                if logger:
                    logger.warning(f"Failed source file not found for {rel_err}")

            progress.advance(task)

    if logger:
        logger.info("Failed file relocation finished.")
    return total
