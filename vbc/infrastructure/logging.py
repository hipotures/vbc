import logging
from pathlib import Path

def setup_logging(output_dir: Path, debug: bool = False) -> logging.Logger:
    """
    Setup logging configuration for VBC.

    Creates output directory and compression.log file.
    Returns configured logger instance.

    Args:
        output_dir: Directory where log file will be created
        debug: If True, enable DEBUG level logging with detailed timings
    """
    # Create output directory
    output_dir.mkdir(exist_ok=True)

    # Setup log file
    log_file = output_dir / "compression.log"

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
