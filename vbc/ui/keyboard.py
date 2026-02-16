import sys
import threading
import termios
import tty
import select
from typing import Optional
from vbc.infrastructure.event_bus import EventBus
from vbc.domain.events import (
    ActionMessage,
    Event,
    InterruptRequested,
    RefreshRequested,
    RequestShutdown,
    ThreadControlEvent,
)

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
    tab: Optional[str] = None  # "settings" | "io" | "reference" | "shortcuts" | "tui" | "logs" | None

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
    
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self):
        """Main loop for the listener thread."""
        if not sys.stdin.isatty():
            return

        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())

            while not self._stop_event.is_set():
                if sys.stdin in select.select([sys.stdin], [], [], 0.1)[0]:
                    key = sys.stdin.read(1)

                    if key in ('.', '>'):
                        self.event_bus.publish(ThreadControlEvent(change=1))
                    elif key in (',', '<'):
                        self.event_bus.publish(ThreadControlEvent(change=-1))
                    elif key in ('S', 's'):
                        self.event_bus.publish(RequestShutdown())
                    elif key in ('R', 'r'):
                        self.event_bus.publish(RefreshRequested())
                        # Immediate feedback (like old vbc.py line 787)
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
                    elif key in ('T', 't'):
                        self.event_bus.publish(ToggleOverlayTab(tab="tui"))
                    elif key == '[':
                        self.event_bus.publish(CycleLogsPage(direction=-1))
                    elif key == ']':
                        self.event_bus.publish(CycleLogsPage(direction=1))
                    elif key == '\t':  # Tab key
                        self.event_bus.publish(CycleOverlayTab(direction=1))
                    elif key in ('D', 'd'):
                        self.event_bus.publish(CycleOverlayDim(direction=1))
                    elif key in ('W', 'w'):
                        self.event_bus.publish(CycleSparklinePreset(direction=1))
                    elif key in ('P', 'p'):
                        self.event_bus.publish(CycleSparklinePalette(direction=1))
                    elif key in ('G', 'g'):
                        self.event_bus.publish(RotateGpuMetric())
                    elif key == '\x1b':
                        self.event_bus.publish(CloseOverlay())
                    elif key == '\x03':
                        # Ctrl+C detected - signal orchestrator to stop
                        self.event_bus.publish(InterruptRequested())
                        # Restore terminal and exit listener thread
                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                        break
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
