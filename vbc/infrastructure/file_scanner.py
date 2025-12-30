import os
from pathlib import Path
from typing import List, Generator
from vbc.domain.models import VideoFile

class FileScanner:
    """Recursively scans for video files in a directory."""
    
    def __init__(self, extensions: List[str], min_size_bytes: int = 0):
        self.extensions = [(ext if ext.startswith(".") else f".{ext}").lower() for ext in extensions]
        self.min_size_bytes = min_size_bytes

    def scan(self, root_dir: Path) -> Generator[VideoFile, None, None]:
        """Scans the directory and yields VideoFile objects."""
        for root, dirs, files in os.walk(str(root_dir)):
            root_path = Path(root)

            # Skip output directories (ending in _out)
            if root_path.name.endswith("_out"):
                dirs[:] = [] # stop recursion into this branch
                continue

            # Ensure deterministic traversal: sort directories and files
            dirs[:] = sorted(d for d in dirs if not d.endswith("_out"))
            files.sort()

            for file_name in files:
                file_path = root_path / file_name
                
                # Check extension
                if file_path.suffix.lower() not in self.extensions:
                    continue
                
                # Check size
                try:
                    file_size = file_path.stat().st_size
                    if file_size < self.min_size_bytes:
                        continue
                        
                    yield VideoFile(path=file_path, size_bytes=file_size)
                except OSError:
                    # Skip files we can't access
                    continue
