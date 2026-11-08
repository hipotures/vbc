import logging
from datetime import datetime
from pathlib import Path
from typing import Optional


def _rotation_timestamp() -> str:
    """Return a filesystem-safe local timestamp for archived logs."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _rotate_existing_log(log_file: Path) -> Optional[Path]:
    """Archive a non-empty log before a new logging session starts."""
    if not log_file.is_file() or log_file.stat().st_size == 0:
        return None

    timestamp = _rotation_timestamp()
    archive = log_file.with_name(f"{log_file.stem}_{timestamp}{log_file.suffix}")
    collision_index = 1

    while archive.exists():
        archive = log_file.with_name(
            f"{log_file.stem}_{timestamp}_{collision_index}{log_file.suffix}"
        )
        collision_index += 1

    log_file.rename(archive)
    return archive


def setup_logging(output_dir: Path, debug: bool = False, log_path: Optional[Path] = None) -> logging.Logger:
    """
    Setup logging configuration for VBC.

    Creates output directory and compression.log file.
    Returns configured logger instance.

    Args:
        output_dir: Directory where output files are written
        debug: If True, enable DEBUG level logging with detailed timings
        log_path: Optional path to log file (overrides output_dir)
    """
    # Create output directory
    output_dir.mkdir(exist_ok=True)

    # Setup log file
    log_file = Path(log_path) if log_path else (output_dir / "compression.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Close handlers first so an in-process restart can rotate the active file.
    for handler in logging.getLogger().handlers:
        handler.flush()
        handler.close()

    _rotate_existing_log(log_file)

    # Configure logging level
    level = logging.DEBUG if debug else logging.INFO

    # Configure logging
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler(log_file, mode="w")],
        force=True  # Override any existing configuration
    )

    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized: {log_file} (debug={'ON' if debug else 'OFF'})")

    return logger
