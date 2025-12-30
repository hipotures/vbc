import yaml
from pathlib import Path
from .models import AppConfig

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
