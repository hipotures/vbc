import yaml
from pathlib import Path
from typing import List
from .models import AppConfig, DemoConfig

def load_config(config_path: Path) -> AppConfig:
    """Loads YAML config and parses it into AppConfig Pydantic model."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f) or {}

    autorotate = data.get("autorotate")
    if isinstance(autorotate, dict) and "patterns" not in autorotate:
        data["autorotate"] = {"patterns": autorotate}
    
    # Existing vbc.yaml structure has 'general' and 'autorotate' at root
    return AppConfig(**data)

def save_dirs_config(config_path: Path, input_dirs: List[str], disabled_input_dirs: List[str]) -> None:
    """Update input_dirs and disabled_input_dirs fields in the YAML config file.

    Reads the existing YAML, updates only the two fields, and writes back.
    All other YAML content is preserved.

    Args:
        config_path: Path to the vbc.yaml config file.
        input_dirs: New list of active input directories.
        disabled_input_dirs: New list of disabled input directories.
    """
    if not config_path.exists():
        return

    with open(config_path, 'r') as f:
        data = yaml.safe_load(f) or {}

    data['input_dirs'] = input_dirs
    if disabled_input_dirs:
        data['disabled_input_dirs'] = disabled_input_dirs
    elif 'disabled_input_dirs' in data:
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
