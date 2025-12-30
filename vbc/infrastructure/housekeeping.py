import os
from pathlib import Path

class HousekeepingService:
    """Service for cleaning up temporary files and error markers."""
    
    def cleanup_temp_files(self, directory: Path):
        """Recursively removes all .tmp files in the directory."""
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.endswith(".tmp"):
                    try:
                        (Path(root) / file).unlink()
                    except OSError:
                        pass

    def cleanup_error_markers(self, directory: Path):
        """Recursively removes all .err files in the directory."""
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.endswith(".err"):
                    try:
                        (Path(root) / file).unlink()
                    except OSError:
                        pass
