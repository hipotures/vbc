import yaml
from pathlib import Path
from typing import Any, Dict, List
from .models import AppConfig, DemoConfig


def _validate_input_dirs_schema(data: Dict[str, Any]) -> None:
    """Reject legacy input_dirs schema in config files."""
    if "disabled_input_dirs" in data:
        raise ValueError(
            "Legacy key 'disabled_input_dirs' is no longer supported. "
            "Use input_dirs entries with {path, enabled} only."
        )

    raw_input_dirs = data.get("input_dirs")
    if raw_input_dirs is None:
        return
    if not isinstance(raw_input_dirs, list):
        raise ValueError("input_dirs must be a list.")
    if any(isinstance(entry, str) for entry in raw_input_dirs):
        raise ValueError(
            "Legacy input_dirs list[str] format is no longer supported. "
            "Use: input_dirs: [{path: /videos, enabled: true}]"
        )
    if any(not isinstance(entry, dict) for entry in raw_input_dirs):
        raise ValueError("input_dirs entries must be objects with keys: path, enabled.")


def load_config(config_path: Path) -> AppConfig:
    """Loads YAML config and parses it into AppConfig Pydantic model."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f) or {}

    autorotate = data.get("autorotate")
    if isinstance(autorotate, dict) and "patterns" not in autorotate:
        data["autorotate"] = {"patterns": autorotate}

    _validate_input_dirs_schema(data)
    
    # Existing vbc.yaml structure has 'general' and 'autorotate' at root
    return AppConfig(**data)

def save_dirs_config(config_path: Path, input_dirs: List[Dict[str, Any]]) -> None:
    """Update input_dirs field in the YAML config file.

    Reads the existing YAML, updates only the input_dirs field, and writes back.
    All other YAML content is preserved.

    Args:
        config_path: Path to the vbc.yaml config file.
        input_dirs: New ordered list of input directory objects.
    """
    if not config_path.exists():
        return

    with open(config_path, 'r') as f:
        data = yaml.safe_load(f) or {}

    data['input_dirs'] = input_dirs
    if 'disabled_input_dirs' in data:
        del data['disabled_input_dirs']

    with open(config_path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def load_demo_config(config_path: Path) -> DemoConfig:
    """Loads YAML config and parses it into DemoConfig Pydantic model."""
    if not config_path.exists():
        raise FileNotFoundError(f"Demo config file not found: {config_path}")

    with open(config_path, 'r') as f:
        data = yaml.safe_load(f) or {}

    return DemoConfig(**data)
