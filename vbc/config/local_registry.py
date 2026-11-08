"""Registry for hierarchical local VBC.YAML configuration files.

Manages discovery and resolution of directory-specific config overrides.
Local configs apply to all files in a directory and its subdirectories.
"""

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from vbc.config.overrides import LOCAL_CONFIG_FILENAME, load_local_config_data


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalConfigEntry:
    """A discovered local VBC.YAML configuration.

    Attributes:
        path: Absolute path to VBC.YAML file.
        directory: Directory containing VBC.YAML.
        data: Parsed and validated config data (dict).
        depth: Directory depth for hierarchy resolution (number of path parts).
    """

    path: Path
    directory: Path
    data: Dict[str, Any]
    depth: int


class LocalConfigRegistry:
    """Thread-safe registry for local VBC.YAML configurations.

    Manages discovery and hierarchical resolution of local config files.
    Nearest ancestor VBC.YAML wins (child overrides parent).
    """

    def __init__(self):
        """Initialize empty registry."""
        self._configs: Dict[Path, LocalConfigEntry] = {}
        self._lock = threading.Lock()
        self._logger = logging.getLogger(__name__)

    def register(self, config_path: Path, config_data: Dict[str, Any]) -> None:
        """Register a discovered VBC.YAML file.

        Args:
            config_path: Absolute path to VBC.YAML file.
            config_data: Parsed and validated config dictionary.
        """
        directory = config_path.parent
        depth = len(directory.parts)

        entry = LocalConfigEntry(
            path=config_path,
            directory=directory,
            data=config_data,
            depth=depth,
        )

        with self._lock:
            self._configs[directory] = entry
            self._logger.debug(
                "Registered local config: %s (depth=%d)", config_path, depth
            )

    def get_applicable_config(self, file_path: Path) -> Optional[LocalConfigEntry]:
        """Get nearest ancestor VBC.YAML for a file path.

        Walks up from file's parent directory to find first matching VBC.YAML.

        Args:
            file_path: Absolute path to video file.

        Returns:
            LocalConfigEntry if found, None otherwise.
        """
        current = file_path.parent

        with self._lock:
            # Walk up directory tree
            while True:
                if current in self._configs:
                    entry = self._configs[current]
                    self._logger.debug(
                        "Found applicable config for %s: %s", file_path, entry.path
                    )
                    return entry

                # Stop at filesystem root
                if current.parent == current:
                    break

                current = current.parent

        self._logger.debug("No local config found for %s", file_path)
        return None

    def build_from_discovery(self, root_dirs: List[Path]) -> None:
        """Scan directory trees and register all VBC.YAML files.

        Args:
            root_dirs: List of root directories to scan recursively.
        """
        for root_dir in root_dirs:
            if not root_dir.exists():
                self._logger.warning("Root directory does not exist: %s", root_dir)
                continue

            if not root_dir.is_dir():
                self._logger.warning("Root path is not a directory: %s", root_dir)
                continue

            self._logger.debug("Scanning for VBC.YAML files in: %s", root_dir)
            self._scan_directory(root_dir)

    def _scan_directory(self, root_dir: Path) -> None:
        """Recursively scan directory for VBC.YAML files.

        Args:
            root_dir: Directory to scan.
        """
        for dirpath, _, filenames in os.walk(root_dir):
            if LOCAL_CONFIG_FILENAME in filenames:
                config_path = Path(dirpath) / LOCAL_CONFIG_FILENAME

                try:
                    config_data = load_local_config_data(config_path)

                    # Only register if data is non-empty (load_local_config_data returns {} on error)
                    if config_data:
                        self.register(config_path, config_data)
                    else:
                        self._logger.warning(
                            "Skipping invalid VBC.YAML at %s", config_path
                        )

                except Exception as exc:
                    self._logger.warning(
                        "Failed to load VBC.YAML at %s: %s", config_path, exc
                    )
