import os
import sys
import threading
import termios
import tty
import select
from typing import Optional, TYPE_CHECKING
from vbc.infrastructure.event_bus import EventBus
from vbc.domain.events import (
    ActionMessage,
    Event,
    InterruptRequested,
    RefreshRequested,
    RequestShutdown,
    ThreadControlEvent,
    DirsCursorMove,
    DirsSwapSelected,
    DirsToggleSelected,
    DirsEnterAddMode,
    DirsMarkDelete,
    DirsInputChar,
    DirsConfirmAdd,
    DirsCancelInput,
    DirsApplyChanges,
)

if TYPE_CHECKING:
    from vbc.ui.state import UIState

# Deprecated overlay events (kept for compatibility)
class ToggleConfig(Event):
    """DEPRECATED: Use ToggleOverlayTab instead. Event emitted when user toggles config display (Key 'C')."""
    pass

class ToggleLegend(Event):
    """DEPRECATED: Use ToggleOverlayTab instead. Event emitted when user toggles legend display (Key 'L')."""
    pass

class ToggleMenu(Event):
    """DEPRECATED: Use ToggleOverlayTab instead. Event emitted when user toggles menu display (Key 'M')."""
    pass

class HideConfig(Event):
    """DEPRECATED: Use CloseOverlay instead. Event emitted when user closes config display (Esc)."""
    pass

# New tabbed overlay events
class ToggleOverlayTab(Event):
    """Event emitted to toggle overlay with optional tab selection."""
    tab: Optional[str] = None  # "settings" | "io" | "dirs" | "reference" | "shortcuts" | "tui" | "logs" | None

class CycleOverlayTab(Event):
    """Event emitted to cycle through overlay tabs."""
    direction: int = 1  # 1=next, -1=previous

class CycleLogsPage(Event):
    """Event emitted to cycle logs page in Logs tab."""
    direction: int = 1  # 1=next, -1=previous

class CloseOverlay(Event):
    """Event emitted to close the overlay."""
    pass

class CycleOverlayDim(Event):
    """Event emitted to cycle overlay background dimming level."""
    direction: int = 1  # 1=next, -1=previous

class RotateGpuMetric(Event):
    """Event emitted when user rotates GPU sparkline metric (Key 'G')."""
    pass

class CycleSparklinePreset(Event):
    """Event emitted to cycle GPU sparkline preset (Key 'W')."""
    direction: int = 1  # 1=next, -1=previous

class CycleSparklinePalette(Event):
    """Event emitted to cycle GPU sparkline palette (Key 'P')."""
    direction: int = 1  # 1=next, -1=previous

class KeyboardListener:
    """Listens for keyboard input in a background thread."""

    def __init__(self, event_bus: EventBus, state: Optional["UIState"] = None):
        self.event_bus = event_bus
        self.state = state
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _dirs_active(self) -> bool:
        """Return True when the Dirs overlay tab is currently shown."""
        return (
            self.state is not None
            and self.state.show_overlay
            and self.state.active_tab == "dirs"
        )

    def _dirs_input_mode(self) -> bool:
        """Return True when the Dirs add-path input mode is active."""
        return self._dirs_active() and self.state.dirs_input_mode  # type: ignore[union-attr]

    def _dirs_has_pending_changes(self) -> bool:
        """Return True when Dirs tab has staged changes awaiting apply."""
        if self.state is None:
            return False
        checker = getattr(self.state, "dirs_has_pending_changes", None)
        if callable(checker):
            return bool(checker())
        return bool(
            getattr(self.state, "dirs_pending_add", None)
            or getattr(self.state, "dirs_pending_remove", None)
            or getattr(self.state, "dirs_pending_toggle", None)
            or getattr(self.state, "dirs_pending_order", None)
        )

    def _try_read(self, fd: int, timeout: float = 0.1) -> Optional[str]:
        """Non-blocking raw read directly from fd (bypasses Python's internal buffer).

        Using os.read(fd) avoids a known issue where Python's TextIOWrapper buffers
        multiple bytes from a single OS read (e.g. the full escape sequence \\x1b[A),
        making select.select think the fd is empty even though bytes are available.
        """
        if fd in select.select([fd], [], [], timeout)[0]:
            try:
                b = os.read(fd, 1)
                return b.decode('utf-8', errors='replace') if b else None
            except OSError:
                return None
        return None

    @staticmethod
    def _is_csi_final(ch: str) -> bool:
        """Return True for a CSI final byte (ASCII range 0x40-0x7E)."""
        return len(ch) == 1 and '@' <= ch <= '~'

    def _read_csi_sequence(self, fd: int, first: str) -> str:
        """Read the rest of a CSI sequence after ESC[ and return full payload.

        Examples:
        - Arrow up:      "A"
        - Delete:        "3~"
        - Shift+ArrowUp: "1;2A"
        """
        seq = first
        if self._is_csi_final(first):
            return seq

        # Read bounded continuation bytes to avoid blocking on malformed input.
        for _ in range(16):
            nxt = self._try_read(fd, 0.02)
            if nxt is None:
                break
            seq += nxt
            if self._is_csi_final(nxt):
                break
        return seq

    def _handle_escape(self, fd: int) -> None:
        """Handle \\x1b — either a plain Esc key or the start of an escape sequence.

        Uses os.read via _try_read to avoid Python buffer / select mismatch.
        """
        seq1 = self._try_read(fd, 0.1)
        if seq1 is None:
            # Plain Esc key
            if self._dirs_input_mode():
                self.event_bus.publish(DirsCancelInput())
            else:
                self.event_bus.publish(CloseOverlay())
            return

        if seq1 != '[':
            # Unknown sequence starting with \x1b + other — treat as Esc
            if self._dirs_input_mode():
                self.event_bus.publish(DirsCancelInput())
            else:
                self.event_bus.publish(CloseOverlay())
            return

        # We have \x1b[ — CSI sequence
        seq2 = self._try_read(fd, 0.1)
        if seq2 is None:
            return  # Incomplete sequence, ignore
        seq = self._read_csi_sequence(fd, seq2)

        if self._dirs_input_mode():
            # Ignore all escape sequences in input mode
            return

        if seq == 'A':  # Up arrow
            if self._dirs_active():
                self.event_bus.publish(DirsCursorMove(direction=-1))
        elif seq == 'B':  # Down arrow
            if self._dirs_active():
                self.event_bus.publish(DirsCursorMove(direction=1))
        elif seq == '3~':  # Delete key: \x1b[3~
            if self._dirs_active():
                self.event_bus.publish(DirsMarkDelete())
        elif self._dirs_active() and ';2' in seq and seq.endswith(('A', 'B')):
            # Shift+Arrow in Dirs tab swaps current row with adjacent row.
            direction = -1 if seq.endswith('A') else 1
            self.event_bus.publish(DirsSwapSelected(direction=direction))
            return
        elif seq.endswith('A') or seq.endswith('B'):
            # Other modified arrows are parsed and consumed to prevent key leaks.
            return
        # All other sequences: silently ignored

    def _run(self):
        """Main loop for the listener thread."""
        if not sys.stdin.isatty():
            return

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(fd)

            while not self._stop_event.is_set():
                if fd in select.select([fd], [], [], 0.1)[0]:
                    try:
                        raw = os.read(fd, 1)
                    except OSError:
                        continue
                    if not raw:
                        continue
                    key = raw.decode('utf-8', errors='replace')

                    # ── Escape / CSI sequences ─────────────────────────────
                    if key == '\x1b':
                        self._handle_escape(fd)
                        continue

                    # ── Ctrl+C (interrupt) ─────────────────────────────────
                    if key == '\x03':
                        self.event_bus.publish(InterruptRequested())
                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                        break

                    # ── Dirs add-path input mode ───────────────────────────
                    if self._dirs_input_mode():
                        if key in ('\r', '\n'):
                            self.event_bus.publish(DirsConfirmAdd())
                        elif key in ('\x7f', '\x08'):  # Backspace
                            self.event_bus.publish(DirsInputChar(char='\x7f'))
                        elif key.isprintable():
                            self.event_bus.publish(DirsInputChar(char=key))
                        # All other keys silently ignored in input mode
                        continue

                    # ── Dirs tab non-input mode ────────────────────────────
                    if self._dirs_active():
                        if key == ' ':
                            self.event_bus.publish(DirsToggleSelected())
                            continue
                        elif key in ('A', 'a'):
                            self.event_bus.publish(DirsEnterAddMode())
                            continue
                        elif key in ('\x7f', '\x08'):  # Backspace/DEL → mark delete
                            self.event_bus.publish(DirsMarkDelete())
                            continue
                        elif key in ('S', 's'):
                            # In dirs tab: S applies pending changes (if any)
                            has_pending = self._dirs_has_pending_changes()
                            if has_pending:
                                self.event_bus.publish(DirsApplyChanges())
                            else:
                                self.event_bus.publish(ActionMessage(message="No pending Dirs changes"))
                            continue
                        # Other keys fall through to normal global handlers below

                    # ── Global key handlers ────────────────────────────────
                    if key in ('.', '>'):
                        self.event_bus.publish(ThreadControlEvent(change=1))
                    elif key in (',', '<'):
                        self.event_bus.publish(ThreadControlEvent(change=-1))
                    elif key in ('S', 's'):
                        if not self._dirs_active():
                            self.event_bus.publish(RequestShutdown())
                    elif key in ('R', 'r'):
                        self.event_bus.publish(RefreshRequested())
                        self.event_bus.publish(ActionMessage(message="REFRESH requested"))
                    elif key in ('C', 'c'):
                        self.event_bus.publish(ToggleOverlayTab(tab="settings"))
                    elif key in ('L', 'l'):
                        self.event_bus.publish(ToggleOverlayTab(tab="logs"))
                    elif key in ('E', 'e'):
                        self.event_bus.publish(ToggleOverlayTab(tab="reference"))
                    elif key in ('M', 'm'):
                        self.event_bus.publish(ToggleOverlayTab(tab="shortcuts"))
                    elif key in ('F', 'f'):
                        self.event_bus.publish(ToggleOverlayTab(tab="io"))
                    elif key in ('D', 'd'):
                        self.event_bus.publish(ToggleOverlayTab(tab="dirs"))
                    elif key in ('T', 't'):
                        self.event_bus.publish(ToggleOverlayTab(tab="tui"))
                    elif key in ('I', 'i'):
                        self.event_bus.publish(CycleOverlayDim(direction=1))
                    elif key == '[':
                        self.event_bus.publish(CycleLogsPage(direction=-1))
                    elif key == ']':
                        self.event_bus.publish(CycleLogsPage(direction=1))
                    elif key == '\t':  # Tab key
                        self.event_bus.publish(CycleOverlayTab(direction=1))
                    elif key in ('W', 'w'):
                        self.event_bus.publish(CycleSparklinePreset(direction=1))
                    elif key in ('P', 'p'):
                        self.event_bus.publish(CycleSparklinePalette(direction=1))
                    elif key in ('G', 'g'):
                        self.event_bus.publish(RotateGpuMetric())
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

    def start(self):
        """Starts the listener thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stops the listener thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
