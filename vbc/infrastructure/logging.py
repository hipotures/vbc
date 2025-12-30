import logging
from pathlib import Path
from typing import Optional

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

    # Configure logging level
    level = logging.DEBUG if debug else logging.INFO

    # Configure logging
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler(log_file)],
        force=True  # Override any existing configuration
    )

    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized: {log_file} (debug={'ON' if debug else 'OFF'})")

    return logger
