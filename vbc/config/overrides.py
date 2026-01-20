import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import yaml

from vbc.config.models import AppConfig
from vbc.domain.models import ConfigSource
from vbc.infrastructure.ffmpeg import replace_quality_value

if TYPE_CHECKING:
    from vbc.config.local_registry import LocalConfigRegistry

LOCAL_CONFIG_FILENAME = "VBC.YAML"

_ALLOWED_ROOT_KEYS = {"general", "gpu_encoder", "cpu_encoder", "autorotate", "cq"}
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
    # Extract cq override before merging (handled specially)
    cq_override = override_data.pop("cq", None) if "cq" in override_data else None

    # Handle encoder args specially - extract quality and apply via replace_quality_value
    override_data = _process_encoder_overrides(base_config, override_data)

    merged = _deep_merge_dicts(base_config.model_dump(), override_data)
    config = AppConfig(**merged)

    # Apply root-level cq override (applies to all encoders)
    if cq_override is not None:
        config.gpu_encoder.common_args = replace_quality_value(config.gpu_encoder.common_args, cq_override)
        config.gpu_encoder.advanced_args = replace_quality_value(config.gpu_encoder.advanced_args, cq_override)
        config.cpu_encoder.common_args = replace_quality_value(config.cpu_encoder.common_args, cq_override)
        config.cpu_encoder.advanced_args = replace_quality_value(config.cpu_encoder.advanced_args, cq_override)

    if cli_overrides:
        cli_overrides.apply(config)
    return config


def _process_encoder_overrides(base_config: AppConfig, override_data: Dict[str, Any]) -> Dict[str, Any]:
    """Process encoder overrides - extract quality values and apply to base args.

    Instead of replacing entire args lists, extracts quality values from override
    and applies them to base config args using replace_quality_value().
    """
    from vbc.infrastructure.ffmpeg import extract_quality_value

    result = dict(override_data)

    for encoder_key in ("gpu_encoder", "cpu_encoder"):
        if encoder_key not in result:
            continue

        encoder_override = result[encoder_key]
        if not isinstance(encoder_override, dict):
            continue

        base_encoder = getattr(base_config, encoder_key)

        for args_key in ("common_args", "advanced_args"):
            if args_key not in encoder_override:
                continue

            override_args = encoder_override[args_key]
            if not isinstance(override_args, list):
                continue

            # Extract quality value from override args
            override_quality = extract_quality_value(override_args)
            if override_quality is not None:
                # Apply quality to base args instead of replacing
                base_args = getattr(base_encoder, args_key)
                encoder_override[args_key] = replace_quality_value(base_args, override_quality)

    return result


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

    # Pass through cq override (handled specially in merge_local_config)
    if "cq" in data:
        cq_val = data["cq"]
        if isinstance(cq_val, int) and 0 <= cq_val <= 63:
            filtered["cq"] = cq_val
        else:
            _logger.warning("Invalid cq value in %s: %s (must be int 0-63)", path, cq_val)

    return filtered


def _deep_merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def build_job_config(
    base_config: AppConfig,
    local_registry: Optional["LocalConfigRegistry"],
    file_path: Path,
    cli_overrides: Optional[CliConfigOverrides],
) -> Tuple[AppConfig, ConfigSource]:
    """Build config for a specific job with proper hierarchy.

    Applies configuration overrides in priority order:
    1. Global config (base_config)
    2. Local VBC.YAML (if exists for file's directory)
    3. CLI arguments (if provided)

    Args:
        base_config: Global configuration from conf/vbc.yaml.
        local_registry: Registry of local VBC.YAML files (optional).
        file_path: Path to video file being processed.
        cli_overrides: CLI argument overrides (optional).

    Returns:
        Tuple of (merged_config, config_source_indicator)
        - merged_config: Final configuration with all overrides applied
        - config_source_indicator: Highest priority source (GLOBAL, LOCAL, or CLI)
    """
    # Start with global config (deep copy to avoid mutation)
    config = base_config.model_copy(deep=True)
    source = ConfigSource.GLOBAL

    # Apply local config if exists
    if local_registry:
        local_entry = local_registry.get_applicable_config(file_path)
        if local_entry:
            config = merge_local_config(config, local_entry.data, None)
            source = ConfigSource.LOCAL

    # Apply CLI overrides (highest priority)
    if cli_overrides and cli_overrides.has_overrides:
        cli_overrides.apply(config)
        source = ConfigSource.CLI

    return config, source
