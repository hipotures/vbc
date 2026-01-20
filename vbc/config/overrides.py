import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from vbc.config.models import AppConfig
from vbc.infrastructure.ffmpeg import replace_quality_value

LOCAL_CONFIG_FILENAME = "VBC.YAML"

_ALLOWED_ROOT_KEYS = {"general", "gpu_encoder", "cpu_encoder", "autorotate"}
_ALLOWED_GENERAL_KEYS = {
    "gpu",
    "cpu_fallback",
    "ffmpeg_cpu_threads",
    "copy_metadata",
    "use_exif",
    "filter_cameras",
    "dynamic_cq",
    "extensions",
    "min_size_bytes",
    "clean_errors",
    "skip_av1",
    "manual_rotation",
    "min_compression_ratio",
    "debug",
}

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CliConfigOverrides:
    threads: Optional[int] = None
    quality: Optional[int] = None
    gpu: Optional[bool] = None
    queue_sort: Optional[str] = None
    queue_seed: Optional[int] = None
    log_path: Optional[str] = None
    clean_errors: bool = False
    skip_av1: bool = False
    min_size: Optional[int] = None
    min_ratio: Optional[float] = None
    camera: Optional[List[str]] = None
    debug: bool = False
    rotate_180: bool = False

    @property
    def has_overrides(self) -> bool:
        return any(
            value is not None
            for value in (
                self.threads,
                self.quality,
                self.gpu,
                self.queue_sort,
                self.queue_seed,
                self.log_path,
                self.min_size,
                self.min_ratio,
                self.camera,
            )
        ) or any((self.clean_errors, self.skip_av1, self.debug, self.rotate_180))

    def apply(self, config: AppConfig) -> None:
        if self.threads is not None:
            config.general.threads = self.threads
        if self.quality is not None:
            config.gpu_encoder.common_args = replace_quality_value(config.gpu_encoder.common_args, self.quality)
            config.gpu_encoder.advanced_args = replace_quality_value(config.gpu_encoder.advanced_args, self.quality)
            config.cpu_encoder.common_args = replace_quality_value(config.cpu_encoder.common_args, self.quality)
            config.cpu_encoder.advanced_args = replace_quality_value(config.cpu_encoder.advanced_args, self.quality)
        if self.gpu is not None:
            config.general.gpu = self.gpu
        if self.queue_sort is not None:
            config.general.queue_sort = self.queue_sort
        if self.queue_seed is not None:
            config.general.queue_seed = self.queue_seed
        if self.log_path is not None:
            config.general.log_path = str(self.log_path)
        if self.clean_errors:
            config.general.clean_errors = True
        if self.skip_av1:
            config.general.skip_av1 = True
        if self.min_size is not None:
            config.general.min_size_bytes = self.min_size
        if self.min_ratio is not None:
            config.general.min_compression_ratio = self.min_ratio
        if self.camera is not None:
            config.general.filter_cameras = self.camera
        if self.debug:
            config.general.debug = True
        if self.rotate_180:
            config.general.manual_rotation = 180


def normalize_extensions(extensions: List[str]) -> List[str]:
    return [ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions]


def load_local_config_data(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception as exc:
        _logger.warning("Failed to read local config %s: %s", path, exc)
        return {}

    if not isinstance(data, dict):
        _logger.warning("Local config %s must be a mapping; ignoring", path)
        return {}

    autorotate = data.get("autorotate")
    if isinstance(autorotate, dict) and "patterns" not in autorotate:
        data["autorotate"] = {"patterns": autorotate}

    return _filter_local_overrides(data, path)


def merge_local_config(
    base_config: AppConfig,
    override_data: Dict[str, Any],
    cli_overrides: Optional[CliConfigOverrides],
) -> AppConfig:
    merged = _deep_merge_dicts(base_config.model_dump(), override_data)
    config = AppConfig(**merged)
    if cli_overrides:
        cli_overrides.apply(config)
    return config


def _filter_local_overrides(data: Dict[str, Any], path: Path) -> Dict[str, Any]:
    filtered: Dict[str, Any] = {}
    ignored_root = set(data.keys()) - _ALLOWED_ROOT_KEYS
    if ignored_root:
        _logger.warning(
            "Ignoring unsupported local config keys in %s: %s",
            path,
            ", ".join(sorted(ignored_root)),
        )

    general = data.get("general")
    if isinstance(general, dict):
        allowed_general = {k: v for k, v in general.items() if k in _ALLOWED_GENERAL_KEYS}
        ignored_general = set(general.keys()) - _ALLOWED_GENERAL_KEYS
        if ignored_general:
            _logger.warning(
                "Ignoring unsupported general keys in %s: %s",
                path,
                ", ".join(sorted(ignored_general)),
            )
        if allowed_general:
            filtered["general"] = allowed_general
    elif general is not None:
        _logger.warning("Local config %s general section must be a mapping; ignoring", path)

    for key in ("gpu_encoder", "cpu_encoder", "autorotate"):
        if key in data:
            filtered[key] = data[key]

    return filtered


def _deep_merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged
