import codecs
import os
import sys
import threading
import termios
import tty
import select
import time
from typing import Optional, TYPE_CHECKING
from vbc.clients.tui.bus import EventBus
from vbc.clients.tui.events import (
    ActionMessage,
    Event,
    QuitRequested,
    RefreshRequested,
    StopRequested,
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
    DirsScanMounts,
)

if TYPE_CHECKING:
    from vbc.clients.tui.state import UIState

# Deprecated overlay events (kept for compatibility)
LOCK_AFTER_S = float(os.environ.get("VBC_TUI_LOCK_AFTER_S", "60"))  # auto-lock after this long without a key; 0 = off
LOCK_FLASH_S = 1.0      # a key pressed while locked shows the lock strongly this long
UNLOCK_QUIET_S = 0.15   # Shift+U unlocks only as a lone key (not inside pasted text)


class ToggleConfig(Event):
    """DEPRECATED: Use ToggleOverlayTab instead. Event emitted when user toggles config display (Key 'P')."""
    pass

class ToggleLegend(Event):
    """DEPRECATED: Use ToggleOverlayTab instead. Event emitted when user toggles legend display (Key 'L')."""
    pass

class ToggleMenu(Event):
    """DEPRECATED: Use ToggleOverlayTab instead. Event emitted when user toggles menu display (Key 'K')."""
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
    """Event emitted to cycle GPU sparkline palette (Key 'O')."""
    direction: int = 1  # 1=next, -1=previous

class KeyboardListener:
    """Listens for keyboard input in a background thread."""

    def __init__(self, event_bus: EventBus, state: Optional["UIState"] = None):
        self.event_bus = event_bus
        self.state = state
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_input_at = time.monotonic()   # last key acted on
        self._last_byte_at = 0.0                  # last byte read from the terminal

    # ── auto-lock and paste guard ───────────────────────────────────────────

    def _locked(self) -> bool:
        return bool(self.state is not None and getattr(self.state, "locked", False))

    def _set_lock(self, locked: bool) -> None:
        if self.state is None:
            return
        self.state.locked = locked
        self.state.lock_flash_until = 0.0
        if not locked:
            self._last_input_at = time.monotonic()
            self.event_bus.publish(ActionMessage(message="Unlocked"))

    def _flash_lock(self) -> None:
        if self.state is not None:
            self.state.lock_flash_until = time.monotonic() + LOCK_FLASH_S

    def _consume_paste(self, fd: int) -> None:
        """Read a bracketed paste up to ESC[201~. Pasted text never acts as keys;
        in the Dirs add-path input it is typed into the path."""
        buf = ""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ch = self._try_read(fd, 0.5)
            if ch is None:
                break
            buf += ch
            if buf.endswith("\x1b[201~"):
                buf = buf[: -len("\x1b[201~")]
                break
        self._last_byte_at = time.monotonic()
        if self._dirs_input_mode() and not self._locked():
            for ch in buf:
                if ch.isprintable():
                    self.event_bus.publish(DirsInputChar(char=ch))

    def _swallow_escape(self, fd: int) -> None:
        """Read and drop one escape sequence (used while locked)."""
        if self._try_read(fd, 0.05) == '[':
            first = self._try_read(fd, 0.05)
            if first is not None and self._read_csi_sequence(fd, first) == '200~':
                self._consume_paste(fd)

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

        if seq == '200~':  # bracketed paste: never interpret pasted text as keys
            self._consume_paste(fd)
            return

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
        try:
            old_settings = termios.tcgetattr(sys.stdin)
        except (OSError, termios.error):
            return
        try:
            tty.setcbreak(fd)
            # Let Ctrl+Q / Ctrl+S reach the app instead of XON/XOFF flow control.
            attrs = termios.tcgetattr(fd)
            attrs[0] &= ~termios.IXON
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            # Bracketed paste: the terminal wraps pasted text in ESC[200~ … ESC[201~.
            sys.stdout.write("\x1b[?2004h")
            sys.stdout.flush()

            while not self._stop_event.is_set():
                if fd not in select.select([fd], [], [], 0.1)[0]:
                    if LOCK_AFTER_S > 0 and not self._locked() and time.monotonic() - self._last_input_at > LOCK_AFTER_S:
                        self._set_lock(True)
                    continue
                if True:
                    try:
                        raw = os.read(fd, 1)
                    except OSError:
                        continue
                    if not raw:
                        continue
                    key = decoder.decode(raw)
                    if not key:
                        continue
                    now = time.monotonic()
                    previous_byte, self._last_byte_at = self._last_byte_at, now

                    # ── Locked: only a lone Shift+U unlocks; Ctrl+C/Ctrl+Q still quit ──
                    if self._locked():
                        if key in ('\x03', '\x11'):
                            self.event_bus.publish(QuitRequested())
                            break
                        if key == '\x1b':
                            self._swallow_escape(fd)
                        elif (key == 'U' and now - previous_byte > UNLOCK_QUIET_S
                              and fd not in select.select([fd], [], [], UNLOCK_QUIET_S)[0]):
                            self._set_lock(False)
                            continue
                        self._flash_lock()
                        continue
                    self._last_input_at = now

                    # ── Escape / CSI sequences ─────────────────────────────
                    if key == '\x1b':
                        self._handle_escape(fd)
                        continue

                    # ── Ctrl+C / Ctrl+Q: quit the client (server keeps running) ──
                    if key in ('\x03', '\x11'):
                        self.event_bus.publish(QuitRequested())
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
                        elif key in ('R', 'r'):
                            # In dirs tab: R looks for new mounts, not for new files
                            self.event_bus.publish(DirsScanMounts())
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
                        # Stop processing (the server keeps running; R resumes).
                        self.event_bus.publish(StopRequested())
                    elif key in ('Q', 'q'):
                        self.event_bus.publish(QuitRequested())
                        break
                    elif key in ('R', 'r'):
                        self.event_bus.publish(RefreshRequested())
                        self.event_bus.publish(ActionMessage(message="REFRESH requested"))
                    elif key in ('P', 'p'):
                        self.event_bus.publish(ToggleOverlayTab(tab="settings"))
                    elif key in ('L', 'l'):
                        self.event_bus.publish(ToggleOverlayTab(tab="logs"))
                    elif key in ('E', 'e'):
                        self.event_bus.publish(ToggleOverlayTab(tab="reference"))
                    elif key in ('K', 'k'):
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
                    elif key in ('O', 'o'):
                        self.event_bus.publish(CycleSparklinePalette(direction=1))
                    elif key in ('G', 'g'):
                        self.event_bus.publish(RotateGpuMetric())
        except (OSError, termios.error):
            return
        finally:
            try:
                sys.stdout.write("\x1b[?2004l")
                sys.stdout.flush()
            except OSError:
                pass
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            except (OSError, termios.error):
                pass

    def start(self):
        """Starts the listener thread."""
        if self._thread is not None and getattr(self._thread, "is_alive", lambda: False)():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stops the listener thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
