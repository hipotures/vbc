import os
from pathlib import Path
from typing import List, Tuple, Optional

MAX_INPUT_DIRS = 50
MAX_INPUT_DIR_LEN = 150
STATUS_OK = "✓"
STATUS_MISSING = "✗"
STATUS_NO_ACCESS = "⚡"


def _strip_wrapping_quotes(value: str) -> str:
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in ('"', "'"):
        return trimmed[1:-1]
    return trimmed


def normalize_input_dir_entries(entries: List[str]) -> List[str]:
    normalized: List[str] = []
    for entry in entries:
        if entry is None:
            continue
        cleaned = _strip_wrapping_quotes(entry)
        if cleaned:
            normalized.append(cleaned)
    return normalized


def dedupe_preserve_order(entries: List[str]) -> List[str]:
    seen = set()
    deduped: List[str] = []
    for entry in entries:
        if entry not in seen:
            seen.add(entry)
            deduped.append(entry)
    return deduped


def parse_cli_input_dirs(input_dirs_arg: Optional[str]) -> List[str]:
    if input_dirs_arg is None:
        return []
    parts = input_dirs_arg.split(",")
    return normalize_input_dir_entries(parts)


def validate_input_dir_entries(entries: List[str]) -> None:
    if len(entries) > MAX_INPUT_DIRS:
        raise ValueError(f"Too many input directories ({len(entries)}). Max {MAX_INPUT_DIRS}.")
    too_long = [entry for entry in entries if len(entry) > MAX_INPUT_DIR_LEN]
    if too_long:
        raise ValueError(
            f"Input directory path too long (>{MAX_INPUT_DIR_LEN} chars): {too_long[0]}"
        )


def _has_read_access(path: Path) -> bool:
    return os.access(path, os.R_OK | os.X_OK)


def _can_write_output_dir(input_dir: Path) -> bool:
    output_dir = input_dir.with_name(f"{input_dir.name}_out")
    if output_dir.exists():
        if not output_dir.is_dir():
            return False
        return os.access(output_dir, os.W_OK | os.X_OK)
    return os.access(output_dir.parent, os.W_OK | os.X_OK)


def evaluate_input_dirs(entries: List[str]) -> Tuple[List[Path], List[Tuple[str, str]]]:
    valid_dirs: List[Path] = []
    status_entries: List[Tuple[str, str]] = []
    for entry in entries:
        path = Path(entry)
        if not path.exists():
            status_entries.append((STATUS_MISSING, entry))
            continue
        if not _has_read_access(path) or not _can_write_output_dir(path):
            status_entries.append((STATUS_NO_ACCESS, entry))
            continue
        status_entries.append((STATUS_OK, entry))
        valid_dirs.append(path)
    return valid_dirs, status_entries


def render_status_icon(status: str) -> str:
    style = "green" if status == STATUS_OK else "red"
    icon = status if status == STATUS_NO_ACCESS else f"{status} "
    return f"[{style}]{icon}[/]"


def build_input_dir_lines(status_entries: List[Tuple[str, str]]) -> List[str]:
    lines: List[str] = []
    for idx, (status, entry) in enumerate(status_entries):
        lines.append(f"  {render_status_icon(status)}{idx + 1}. {entry}")
    return lines
