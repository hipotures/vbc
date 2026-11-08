"""Plain-text formatting helpers shared by the client commands (and the TUI)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from vbc import contracts as dto


def size(value: Optional[float]) -> str:
    if value is None:
        return "-"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(amount) < 1024.0 or unit == "TB":
            return f"{amount:.1f}{unit}" if unit != "B" else f"{int(amount)}B"
        amount /= 1024.0
    return f"{amount:.1f}TB"


def duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def rate(bytes_per_s: Optional[float]) -> str:
    if bytes_per_s is None:
        return "-"
    return f"{bytes_per_s / 1024 / 1024:.1f} MB/s"


def ago(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{int(seconds // 3600)}h ago"


def dir_kind(entry: "dto.DirStatus") -> str:
    """Short label of an input folder's manifest/watch settings ('' for a plain folder)."""
    parts = []
    if entry.metadata:
        parts.append("manifests")
    if entry.watch:
        mode = entry.watch_mode or "inotify"
        if mode == "polling" and entry.poll_interval_seconds:
            mode += f" {entry.poll_interval_seconds:g}s"
        parts.append(f"watch ({mode})")
    return " · ".join(parts)
