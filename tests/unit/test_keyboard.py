from unittest.mock import MagicMock

from vbc.domain.events import ActionMessage, RefreshRequested
from vbc.ui.keyboard import (
    KeyboardListener,
    RequestShutdown,
    ThreadControlEvent,
    ToggleConfig,
    HideConfig,
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
    """Test _run publishes events for supported keys."""
    keys = ['.', ',', 's', 'r', 'c', '\x1b', '\x03']

    class FakeStdin:
        def isatty(self):
            return True

        def fileno(self):
            return 0

        def read(self, _count):
            return keys.pop(0)

    fake_stdin = FakeStdin()
    monkeypatch.setattr("vbc.ui.keyboard.sys.stdin", fake_stdin)

    def fake_select(_read, _write, _err, _timeout):
        return ([fake_stdin], [], []) if keys else ([], [], [])

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
    assert any(isinstance(e, ToggleConfig) for e in published)
    assert any(isinstance(e, HideConfig) for e in published)
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
