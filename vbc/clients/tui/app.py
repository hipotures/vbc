"""`vbc tui`: compose poller, controller, keyboard and dashboard."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from vbc.clients.api_client import ApiClient
from vbc.clients.tui.bus import EventBus
from vbc.clients.tui.controller import TuiController
from vbc.clients.tui.dashboard import Dashboard
from vbc.clients.tui.keyboard import KeyboardListener
from vbc.clients.tui.poller import Poller
from vbc.clients.tui.state import ClientState

FIRST_SYNC_TIMEOUT_S = 5.0


def default_log_file() -> Path:
    """$XDG_STATE_HOME/vbc/tui.log (default ~/.local/state/vbc/tui.log)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "vbc" / "tui.log"


def run_tui(api: Optional[str] = None, log_file: Optional[str] = None) -> int:
    # Log output must not write into the Live display.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    path = Path(log_file) if log_file else default_log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(path)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    except OSError:
        handler = logging.NullHandler()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    client = ApiClient(api, timeout=10)
    state = ClientState(activity_feed_max_items=5)
    state.server_url = client.base_url
    state.ui_title = f"VBC @ {urlsplit(client.base_url).netloc}"
    bus = EventBus()
    detached = threading.Event()

    poller = Poller(client, state)
    controller = TuiController(bus, state, client, on_detach=detached.set)
    keyboard = KeyboardListener(bus, state=state)
    dashboard = Dashboard(state, panel_height_scale=0.7, max_active_jobs=8)

    poller.start()
    poller.first_sync.wait(FIRST_SYNC_TIMEOUT_S)  # otherwise start DISCONNECTED and keep retrying
    keyboard.start()
    try:
        with dashboard:
            try:
                while not detached.wait(0.3):
                    if state.quit_ready():  # Ctrl+Q: the jobs running at the key press are done
                        break
            except KeyboardInterrupt:  # no TTY: SIGINT instead of the raw Ctrl+C key
                pass
    finally:
        keyboard.stop()
        poller.stop()
        controller.close()
        client.close()
    return 0
