"""Rich ``Panel`` whose titles join the border line.

Top edge (any position):     ──┐ TITLE ┌──
Centred bottom subtitle:     ──┤ TEXT ├──
Bottom edge, left/right:     ──┘ TEXT └──

``title_raw`` / ``subtitle_raw`` draw the text exactly as given (no joiners,
no padding), for an edge assembled by the caller (several labels, tabs).
``tight(label, edge)`` is one such label without spaces: ``┘Esc close└``.
Relies on Rich's ``Panel._title`` / ``_subtitle`` (padded title text).
"""

from __future__ import annotations

from typing import Optional

from rich.panel import Panel as RichPanel
from rich.text import Text

TOP = ("┐", "┌")
CENTER = ("┤", "├")
BOTTOM = ("┘", "└")


def joined(text: Text | str, edge: tuple[str, str]) -> Text:
    """``┘ text └`` (joiners take the border style when the panel draws them)."""
    body = Text(text) if isinstance(text, str) else text
    return Text.assemble(edge[0], " ", body, " ", edge[1], no_wrap=True, end="")


def tight(text: Text | str, edge: tuple[str, str]) -> Text:
    """``┘text└``: no spaces, for rows of adjacent labels (tabs, key hints)."""
    body = Text(text) if isinstance(text, str) else text
    return Text.assemble(edge[0], body, edge[1], no_wrap=True, end="")


class Sized:
    """Builds its renderable for the width it gets (edges assembled to fit)."""

    def __init__(self, build) -> None:
        self.build = build

    def __rich_console__(self, console, options):
        built = self.build(options.max_width)
        # Rich drops the region height for a nested renderable: pass it on.
        if isinstance(built, RichPanel) and built.height is None and options.height:
            built.height = options.height
        yield built


def centred_with_right(name: str, right: Text, width: int) -> Text:
    """Top edge for ``title_raw``: ``──┐ NAME ┌──┐right┌─`` (name centred)."""
    inner = max(0, width - 4)  # corners and one line each side
    label = joined(name, TOP)
    if inner < label.cell_len + right.cell_len + 3:
        return joined(name, TOP)
    left = (inner - label.cell_len) // 2
    mid = max(1, inner - left - label.cell_len - right.cell_len - 1)
    return Text.assemble("─" * left, label, "─" * mid, right, "─", no_wrap=True, end="")


class Panel(RichPanel):
    def __init__(self, *args, title_raw: bool = False, subtitle_raw: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.title_raw = title_raw
        self.subtitle_raw = subtitle_raw

    @staticmethod
    def _raw(padded: Optional[Text]) -> Optional[Text]:
        if padded is None:
            return None
        raw = padded[1:-1]  # a slice is a new Text: set end/no_wrap again
        raw.end = ""
        raw.no_wrap = True
        return raw

    @staticmethod
    def _join(padded: Optional[Text], edge: tuple[str, str]) -> Optional[Text]:
        if padded is None:
            return None
        return Text.assemble(edge[0], padded, edge[1], no_wrap=True, end="")

    @property
    def _title(self) -> Optional[Text]:
        if self.title_raw:
            return self._raw(super()._title)
        return self._join(super()._title, TOP)  # top edge: ┐ TITLE ┌ wherever it sits

    @property
    def _subtitle(self) -> Optional[Text]:
        padded = super()._subtitle
        if self.subtitle_raw:
            return self._raw(padded)
        return self._join(padded, CENTER if self.subtitle_align == "center" else BOTTOM)
