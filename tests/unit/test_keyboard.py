from unittest.mock import MagicMock

from types import SimpleNamespace
from vbc.domain.events import (
    ActionMessage,
    DirsEnterAddMode,
    InterruptRequested,
    RefreshRequested,
    RequestShutdown,
    ThreadControlEvent,
)
from vbc.ui.keyboard import (
    CycleLogsPage,
    CycleOverlayDim,
    KeyboardListener,
    ToggleOverlayTab,
    CloseOverlay,
)
from vbc.infrastructure.event_bus import EventBus

def test_keyboard_listener_initialization():
    """Test that KeyboardListener can be initialized with EventBus."""
    bus = EventBus()
    listener = KeyboardListener(bus)
    assert listener.event_bus is bus
    assert listener._stop_event is not None

def test_keyboard_listener_stop_event():
    """Test that listener has a stop event for thread control."""
    bus = EventBus()
    listener = KeyboardListener(bus)
    assert not listener._stop_event.is_set()

    # Calling stop() should set the event
    listener.stop()
    assert listener._stop_event.is_set()

def test_request_shutdown_event():
    """Test RequestShutdown event can be created."""
    event = RequestShutdown()
    assert event is not None

def test_thread_control_event():
    """Test ThreadControlEvent with change values."""
    increase_event = ThreadControlEvent(change=1)
    decrease_event = ThreadControlEvent(change=-1)

    assert increase_event.change == 1
    assert decrease_event.change == -1


def test_keyboard_listener_run_handles_keys(monkeypatch):
    """Test _run publishes events for supported keys.

    Key changes vs old mapping:
    - D → ToggleOverlayTab(tab="dirs")  (was CycleOverlayDim)
    - I → CycleOverlayDim               (new)
    - \\x1b is a plain Esc → CloseOverlay (fake select returns empty after \\x1b).

    Note: the listener now uses os.read(fd) instead of sys.stdin.read() to avoid
    Python buffer/select mismatch with escape sequences. We patch os.read accordingly.
    """
    keys = ['.', ',', 's', 'r', 'c', 'l', 'e', '[', ']', 'd', 'i', '\x1b', '\x03']

    # After os.read returns \x1b the listener calls _try_read() → select.select.
    # We flag it so fake_select returns "no data" (plain Esc → CloseOverlay).
    after_escape = [False]

    FAKE_FD = 42  # arbitrary fake file descriptor

    class FakeStdin:
        def isatty(self):
            return True

        def fileno(self):
            return FAKE_FD

    fake_stdin = FakeStdin()
    monkeypatch.setattr("vbc.ui.keyboard.sys.stdin", fake_stdin)

    def fake_os_read(_fd, _n):
        ch = keys.pop(0)
        if ch == '\x1b':
            after_escape[0] = True
        return ch.encode('utf-8')

    monkeypatch.setattr("vbc.ui.keyboard.os.read", fake_os_read)

    def fake_select(_read, _write, _err, _timeout):
        if after_escape[0]:
            after_escape[0] = False
            return ([], [], [])
        return ([FAKE_FD], [], []) if keys else ([], [], [])

    monkeypatch.setattr("vbc.ui.keyboard.select.select", fake_select)
    monkeypatch.setattr("vbc.ui.keyboard.termios.tcgetattr", MagicMock(return_value="old"))
    tcset = MagicMock()
    monkeypatch.setattr("vbc.ui.keyboard.termios.tcsetattr", tcset)
    monkeypatch.setattr("vbc.ui.keyboard.tty.setcbreak", MagicMock())

    bus = MagicMock()
    listener = KeyboardListener(bus)
    listener._run()

    published = [call.args[0] for call in bus.publish.call_args_list]

    assert any(isinstance(e, ThreadControlEvent) and e.change == 1 for e in published)
    assert any(isinstance(e, ThreadControlEvent) and e.change == -1 for e in published)
    assert any(isinstance(e, RequestShutdown) for e in published)
    assert any(isinstance(e, RefreshRequested) for e in published)
    assert any(isinstance(e, ActionMessage) and e.message == "REFRESH requested" for e in published)
    assert any(isinstance(e, ToggleOverlayTab) and e.tab == "settings" for e in published)
    assert any(isinstance(e, ToggleOverlayTab) and e.tab == "logs" for e in published)
    assert any(isinstance(e, ToggleOverlayTab) and e.tab == "reference" for e in published)
    # D now opens Dirs tab (was CycleOverlayDim)
    assert any(isinstance(e, ToggleOverlayTab) and e.tab == "dirs" for e in published)
    # I now cycles dim level (was D)
    assert any(isinstance(e, CycleOverlayDim) and e.direction == 1 for e in published)
    assert any(isinstance(e, CycleLogsPage) and e.direction == -1 for e in published)
    assert any(isinstance(e, CycleLogsPage) and e.direction == 1 for e in published)
    assert any(isinstance(e, CloseOverlay) for e in published)
    assert tcset.called


def test_keyboard_listener_run_non_tty_noop(monkeypatch):
    """Test _run exits when stdin is not a TTY."""
    class FakeStdin:
        def isatty(self):
            return False

    fake_stdin = FakeStdin()
    monkeypatch.setattr("vbc.ui.keyboard.sys.stdin", fake_stdin)
    tcget = MagicMock()
    monkeypatch.setattr("vbc.ui.keyboard.termios.tcgetattr", tcget)

    bus = MagicMock()
    listener = KeyboardListener(bus)
    listener._run()

    assert not tcget.called
    assert not bus.publish.called


def test_keyboard_listener_start_stop_joins_thread(monkeypatch):
    """Test start creates a thread and stop joins it."""
    class DummyThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon
            self.started = False
            self.joined = False

        def start(self):
            self.started = True

        def join(self, timeout=None):
            self.joined = True

    monkeypatch.setattr("vbc.ui.keyboard.threading.Thread", DummyThread)

    listener = KeyboardListener(EventBus())
    listener.start()

    assert isinstance(listener._thread, DummyThread)
    assert listener._thread.started

    listener.stop()
    assert listener._thread.joined


def test_shift_arrow_up_in_dirs_does_not_trigger_add_mode(monkeypatch):
    """Shift+Up CSI sequence should be fully consumed and not leak 'A' key."""
    keys = ['\x1b', '[', '1', ';', '2', 'A', '\x03']
    FAKE_FD = 42

    class FakeStdin:
        def isatty(self):
            return True

        def fileno(self):
            return FAKE_FD

    monkeypatch.setattr("vbc.ui.keyboard.sys.stdin", FakeStdin())

    def fake_os_read(_fd, _n):
        ch = keys.pop(0)
        return ch.encode('utf-8')

    monkeypatch.setattr("vbc.ui.keyboard.os.read", fake_os_read)

    def fake_select(_read, _write, _err, _timeout):
        return ([FAKE_FD], [], []) if keys else ([], [], [])

    monkeypatch.setattr("vbc.ui.keyboard.select.select", fake_select)
    monkeypatch.setattr("vbc.ui.keyboard.termios.tcgetattr", MagicMock(return_value="old"))
    monkeypatch.setattr("vbc.ui.keyboard.termios.tcsetattr", MagicMock())
    monkeypatch.setattr("vbc.ui.keyboard.tty.setcbreak", MagicMock())

    bus = MagicMock()
    state = SimpleNamespace(show_overlay=True, active_tab="dirs", dirs_input_mode=False)
    listener = KeyboardListener(bus, state=state)
    listener._run()

    published = [call.args[0] for call in bus.publish.call_args_list]
    assert any(isinstance(e, InterruptRequested) for e in published)
    assert not any(isinstance(e, DirsEnterAddMode) for e in published)
