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


class QuitRequested(Event):
    """Ctrl+C, Ctrl+Q or q: quit the client now (the server keeps running)."""


class RefreshRequested(Event):
    """Key R: ask the server for a rescan."""


class StopRequested(Event):
    """Key S: finish the running jobs, start nothing new (S again cancels)."""


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


class DirsScanMounts(Event):
    """Dirs tab [R]: offer the server's new media mounts as disabled folders."""
    pass
