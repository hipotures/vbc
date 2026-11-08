import os
import shutil
import logging
from pathlib import Path
from typing import Optional

class HousekeepingService:
    """Service for cleaning up temporary files and error markers."""
    
    def _find_source_for_marker(self, input_dir: Path, rel_marker_path: Path) -> Optional[Path]:
        output_rel = rel_marker_path.with_suffix("")
        direct = input_dir / output_rel
        if direct.exists():
            return direct

        base_parent = direct.parent
        if not base_parent.exists():
            return None

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
            return entry
        return None

    def cleanup_output_markers(
        self,
        input_dir: Path,
        output_dir: Path,
        errors_dir: Path,
        clean_errors: bool,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Cleanup .tmp (always) and .err (only if clean_errors) in output_dir.

        If a marker has a corresponding source file in input_dir, delete it.
        Otherwise, move it to errors_dir and log a warning.
        """
        if not output_dir.exists():
            return

        markers = []
        for root, _dirs, files in os.walk(output_dir):
            for file in files:
                if file.endswith(".tmp") or (clean_errors and file.endswith(".err")):
                    markers.append(Path(root) / file)

        if not markers:
            return

        errors_dir.mkdir(parents=True, exist_ok=True)

        for marker in markers:
            try:
                rel_marker = marker.relative_to(output_dir)
            except ValueError:
                rel_marker = Path(marker.name)
            source = self._find_source_for_marker(input_dir, rel_marker)
            if source and source.exists():
                try:
                    marker.unlink()
                except OSError:
                    pass
                continue

            dest = errors_dir / rel_marker
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(marker), str(dest))
            except OSError:
                continue
            if logger:
                logger.warning(
                    f"Moved stale marker without source file: {marker} -> {dest}"
                )
