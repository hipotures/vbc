"""Keyboard / view events of the TUI client (local to the client process).

Copied from vbc_legacy.domain.events (keystroke-level events, which are not
domain events). Commands derived from them are sent to the server over HTTP
by ``controller.TuiController``.
"""

from pydantic import BaseModel


class Event(BaseModel):
    """Base class of TUI-local events."""


class ActionMessage(Event):
    """Transient feedback line (shown for 60 s)."""

    message: str


class InterruptRequested(Event):
    """Ctrl+C in the TUI: detach the client (the server keeps running)."""


class RefreshRequested(Event):
    """Key R: ask the server for a rescan."""


class RequestShutdown(Event):
    """Key S: request (or cancel) a graceful server shutdown."""


class ThreadControlEvent(Event):
    """Keys < / >: change the server thread count by +-1 (sent as an absolute target)."""

    change: int


# ── Dirs tab events ────────────────────────────────────────────────────────────

class DirsCursorMove(Event):
    """Move cursor up/down in the Dirs tab list."""
    direction: int  # -1 = up, +1 = down


class DirsSwapSelected(Event):
    """Swap the entry under cursor with adjacent entry (-1 up, +1 down)."""
    direction: int  # -1 = up, +1 = down


class DirsToggleSelected(Event):
    """Toggle enabled/disabled state of the entry under cursor."""
    pass


class DirsEnterAddMode(Event):
    """Enter add-path input mode in Dirs tab."""
    pass


class DirsMarkDelete(Event):
    """Mark the entry under cursor for deletion (pending)."""
    pass


class DirsInputChar(Event):
    """Append a character to the add-path input buffer.
    Use char='\\x7f' for backspace (removes last character)."""
    char: str


class DirsConfirmAdd(Event):
    """Confirm the current input buffer as a new pending directory."""
    pass


class DirsCancelInput(Event):
    """Cancel add-path input mode without saving."""
    pass


class DirsApplyChanges(Event):
    """Apply all pending Dirs changes, save to YAML, and trigger refresh."""
    pass
