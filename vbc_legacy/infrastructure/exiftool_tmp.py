import logging
import os
from pathlib import Path
from typing import Iterable, List, Optional


EXIFTOOL_TMP_SUFFIX = "_exiftool_tmp"


def exiftool_tmp_path(target_path: Path) -> Path:
    """Return ExifTool's temporary write path for a target file."""
    return target_path.with_name(f"{target_path.name}{EXIFTOOL_TMP_SUFFIX}")


def remove_exiftool_tmp_for_target(
    target_path: Path,
    logger: Optional[logging.Logger] = None,
) -> Optional[Path]:
    """Remove a stale ExifTool temp file for a single target if present."""
    tmp_path = exiftool_tmp_path(target_path)
    if not os.path.lexists(tmp_path):
        return None
    if tmp_path.is_dir() and not tmp_path.is_symlink():
        if logger:
            logger.warning(f"Refusing to remove ExifTool temp directory: {tmp_path}")
        return None
    tmp_path.unlink()
    if logger:
        logger.warning(f"Removed stale ExifTool temp file: {tmp_path}")
    return tmp_path


def cleanup_exiftool_tmp_files(
    roots: Iterable[Path],
    logger: Optional[logging.Logger] = None,
) -> List[Path]:
    """Remove stale ExifTool temp files below output roots."""
    removed: List[Path] = []
    seen_roots = set()
    for root in roots:
        root = Path(root)
        if root in seen_roots:
            continue
        seen_roots.add(root)
        if not root.exists():
            continue
        for tmp_path in root.rglob(f"*{EXIFTOOL_TMP_SUFFIX}"):
            if tmp_path.is_dir() and not tmp_path.is_symlink():
                if logger:
                    logger.warning(f"Refusing to remove ExifTool temp directory: {tmp_path}")
                continue
            if not tmp_path.is_file() and not tmp_path.is_symlink():
                continue
            tmp_path.unlink()
            removed.append(tmp_path)
            if logger:
                logger.warning(f"Removed stale ExifTool temp file: {tmp_path}")
    return removed
