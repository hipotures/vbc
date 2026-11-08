"""Helpers for rendering untrusted text safely in the terminal UI."""

import unicodedata
from typing import Any

from rich.cells import cell_len, get_character_cell_size
from rich.markup import escape


def single_line(value: Any) -> str:
    """Return text that cannot inject terminal control characters or new lines."""
    text = "" if value is None else str(value)
    return "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)


def safe_markup(value: Any) -> str:
    """Return single-line text escaped for interpolation into Rich markup."""
    return escape(single_line(value))


def _take_cells(text: str, width: int, *, from_end: bool = False) -> str:
    """Take as many complete characters as fit in a terminal-cell budget."""
    if width <= 0:
        return ""

    chars = reversed(text) if from_end else iter(text)
    selected: list[str] = []
    used = 0
    for char in chars:
        char_width = get_character_cell_size(char)
        if used + char_width > width:
            break
        selected.append(char)
        used += char_width

    if from_end:
        selected.reverse()
    return "".join(selected)


def truncate_cells(value: Any, max_width: int, *, preserve_end: bool = False) -> str:
    """Truncate text to a terminal-cell width, optionally preserving both ends."""
    text = single_line(value)
    if max_width <= 0:
        return ""
    if cell_len(text) <= max_width:
        return text
    if max_width == 1:
        return "…"

    content_width = max_width - 1
    if not preserve_end:
        return f"{_take_cells(text, content_width)}…"

    left_width = content_width // 2
    right_width = content_width - left_width
    prefix = _take_cells(text, left_width)
    suffix = _take_cells(text, right_width, from_end=True)
    return f"{prefix}…{suffix}"
