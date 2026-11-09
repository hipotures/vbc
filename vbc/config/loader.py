import os
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import ValidationError

from .models import AppConfig, DemoConfig, InputDirEntry, MetadataConfig


def _validate_input_dirs_schema(data: Dict[str, Any]) -> None:
    """Reject legacy input_dirs schema in config files."""
    if "disabled_input_dirs" in data:
        raise ValueError(
            "Legacy key 'disabled_input_dirs' is no longer supported. "
            "Use input_dirs entries with "
            "{path, enabled, metadata?, watch?, watch_mode?, "
            "poll_interval_seconds?, idle_interval?}."
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
        raise ValueError(
            "input_dirs entries must be objects with keys: "
            "path, enabled, metadata?, watch?, watch_mode?, "
            "poll_interval_seconds?, idle_interval?."
        )


def _salvage_input_dirs(data: Dict[str, Any], problems: List[str]) -> None:
    """Keep the server running despite a broken input_dirs entry: an entry that
    does not validate is kept *disabled* (path known) or dropped (no path), and
    the reason goes to ``problems``. Never enables anything."""
    raw = data.get("input_dirs")
    if raw is None:
        return
    if not isinstance(raw, list):
        problems.append(f"input_dirs is not a list ({type(raw).__name__}); no input folders loaded")
        data["input_dirs"] = []
        return
    kept: List[Dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, str):
            entry = {"path": entry, "enabled": False}
            problems.append(f"input_dirs[{index}] {entry['path']!r}: plain string entry; kept disabled")
        if not isinstance(entry, dict) or not entry.get("path"):
            problems.append(f"input_dirs[{index}]: no path; entry ignored")
            continue
        try:
            InputDirEntry.model_validate(entry)
            kept.append(entry)
        except ValidationError as exc:
            reasons = "; ".join(
                f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']} (got {err.get('input')!r})"
                for err in exc.errors()
            )
            problems.append(f"input_dirs[{index}] {entry['path']!r}: {reasons}; folder kept disabled")
            kept.append({"path": str(entry["path"]), "enabled": False})
    data["input_dirs"] = kept


def load_config(config_path: Path, problems: Optional[List[str]] = None) -> AppConfig:
    """Loads YAML config and parses it into AppConfig Pydantic model.

    With ``problems`` (the server), broken input_dirs entries are disabled
    instead of failing the whole load; their reasons are appended there.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f) or {}

    if problems is not None:
        data.pop("disabled_input_dirs", None)
        _salvage_input_dirs(data, problems)
    _validate_input_dirs_schema(data)
    
    # Existing vbc.yaml structure has 'general' and 'autorotate' at root
    return AppConfig(**data)


def load_metadata_config(config_path: Path) -> MetadataConfig:
    """Reload only the hot-swappable metadata policy section."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f) or {}
    return MetadataConfig.model_validate(data.get("metadata") or {})

def save_dirs_config(
    config_path: Path, input_dirs: List[Dict[str, Any]], discovered: Optional[List[str]] = None,
) -> None:
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

    existing_by_path = {
        str(entry.get("path")): entry
        for entry in data.get("input_dirs", [])
        if isinstance(entry, dict) and entry.get("path") is not None
    }
    preserved_entries = []
    for entry in input_dirs:
        path = str(entry.get("path"))
        preserved = dict(existing_by_path.get(path, {}))
        preserved.update(entry)
        preserved_entries.append(preserved)
    data['input_dirs'] = preserved_entries
    if 'disabled_input_dirs' in data:
        del data['disabled_input_dirs']
    if discovered is not None:
        storage = data.get('storage') if isinstance(data.get('storage'), dict) else {}
        storage['discovered'] = list(discovered)
        data['storage'] = storage

    # Write a sibling temp file and rename it over the config: an interruption
    # leaves either the old or the new file, never a truncated one.
    tmp_path = config_path.with_name(config_path.name + '.dirs.tmp')
    try:
        with open(tmp_path, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def load_demo_config(config_path: Path) -> DemoConfig:
    """Loads YAML config and parses it into DemoConfig Pydantic model."""
    if not config_path.exists():
        raise FileNotFoundError(f"Demo config file not found: {config_path}")

    with open(config_path, 'r') as f:
        data = yaml.safe_load(f) or {}

    return DemoConfig(**data)
