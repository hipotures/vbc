"""Rich ``Panel`` whose titles join the border line.

Top edge (any position):     ──┐ TITLE ┌──
Centred bottom subtitle:     ──┤ TEXT ├──
Bottom edge, left/right:     ──┘ TEXT └──

``subtitle_raw=True`` draws a subtitle exactly as given (no joiners, no
padding), for a bottom line assembled by the caller (several labels).
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


class Panel(RichPanel):
    def __init__(self, *args, subtitle_raw: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.subtitle_raw = subtitle_raw

    @staticmethod
    def _join(padded: Optional[Text], edge: tuple[str, str]) -> Optional[Text]:
        if padded is None:
            return None
        return Text.assemble(edge[0], padded, edge[1], no_wrap=True, end="")

    @property
    def _title(self) -> Optional[Text]:
        return self._join(super()._title, TOP)  # top edge: ┐ TITLE ┌ wherever it sits

    @property
    def _subtitle(self) -> Optional[Text]:
        padded = super()._subtitle
        if padded is not None and self.subtitle_raw:
            raw = padded[1:-1]  # a slice is a new Text: set end/no_wrap again
            raw.end = ""
            raw.no_wrap = True
            return raw
        return self._join(padded, CENTER if self.subtitle_align == "center" else BOTTOM)
