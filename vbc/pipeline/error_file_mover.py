import logging
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from rich.progress import Progress, BarColumn, SpinnerColumn, TextColumn, TimeRemainingColumn


def _find_sources_for_error(
    input_dir: Path,
    rel_err_path: Path,
) -> List[Path]:
    output_rel = rel_err_path.with_suffix("")
    direct = input_dir / output_rel
    if direct.exists():
        return [direct]

    base_parent = direct.parent
    if not base_parent.exists():
        return []

    candidates: List[Path] = []
    base_name = output_rel.name
    base_name_lower = base_name.lower()
    base_name_core = Path(base_name).stem
    base_name_core_lower = base_name_core.lower()
    for entry in base_parent.iterdir():
        if not entry.is_file():
            continue
        stem = entry.stem
        stem_lower = stem.lower()
        if (
            stem != base_name
            and stem_lower != base_name_lower
            and stem != base_name_core
            and stem_lower != base_name_core_lower
        ):
            continue
        candidates.append(entry)
    if not candidates:
        return []
    candidates.sort(key=lambda path: path.name.lower())
    return candidates


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
) -> List[Path]:
    """
    Moves failed source files and their error markers to the errors directory.
    Returns a list of paths to the moved video files in the destination (errors) directory.
    """
    error_entries = error_entries or collect_error_entries(
        input_dirs, output_dir_map, errors_dir_map
    )

    total = len(error_entries)
    moved_video_files: List[Path] = []
    
    if total == 0:
        if logger:
            logger.info("No .err files found for failed file relocation.")
        return []

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
                if logger:
                    logger.info(f"Moved error marker: {err_file} -> {dest_err}")

            source_paths = _find_sources_for_error(input_dir, rel_err)
            if source_paths:
                for source_path in source_paths:
                    if not source_path.exists():
                        continue
                    rel_source = source_path.relative_to(input_dir)
                    dest_source = errors_dir / rel_source
                    dest_source.parent.mkdir(parents=True, exist_ok=True)
                    if source_path != dest_source:
                        shutil.move(str(source_path), str(dest_source))
                        moved_video_files.append(dest_source)
                        if logger:
                            logger.info(f"Moved source file: {source_path} -> {dest_source}")
            else:
                if logger:
                    logger.warning(f"Failed source file not found for {rel_err}")

            progress.advance(task)

    if logger:
        logger.info("Failed file relocation finished.")
    
    # Deduplicate in case multiple source variants were moved for one error
    return sorted(list(set(moved_video_files)))
