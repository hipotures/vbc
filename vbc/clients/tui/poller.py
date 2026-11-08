"""Background thread that keeps ``ClientState`` in sync with the server.

Long-polls ``/state`` on ``rev``; on a change it also refetches what the
snapshot says changed (``/queue`` first page, ``/errors`` since the last id,
``/dirs`` + ``/io`` on ``dirs_rev``, ``/config`` on ``config_rev``) and polls
``/gpu`` and ``/io`` at a slow fixed pace. On a lost connection the state is
marked DISCONNECTED and the client keeps retrying; extrapolation continues.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from vbc.clients.api_client import ApiClient, ApiError, ApiUnavailable
from vbc.clients.tui.configview import build_config_lines
from vbc.clients.tui.state import ClientState

logger = logging.getLogger(__name__)

LONG_POLL_S = 5.0
QUEUE_PAGE = 60
GPU_EVERY_S = 5.0
IO_EVERY_S = 10.0
RETRY_S = 2.0


class Poller:
    def __init__(self, client: ApiClient, state: ClientState) -> None:
        self.client = client
        self.state = state
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="vbc-tui-poll", daemon=True)
        self._rev = 0
        self._dirs_rev: Optional[int] = None
        self._config_rev: Optional[int] = None
        self._errors_id = 0
        self._gpu_seq = 0
        self._next_gpu = 0.0
        self._next_io = 0.0
        self.first_sync = threading.Event()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except ApiUnavailable as exc:
                self.state.set_disconnected(exc.reason)
                self._rev = 0  # resynchronise from a full snapshot
                self._stop.wait(RETRY_S)
            except ApiError as exc:
                logger.warning(f"API error while polling: {exc}")
                self.state.set_disconnected(f"server error {exc.status}")
                self._stop.wait(RETRY_S)

    def _tick(self) -> None:
        client, state = self.client, self.state
        snap = client.state() if self._rev == 0 else client.state(since=self._rev, wait=LONG_POLL_S)
        now = time.monotonic()
        if snap is not None:
            self._rev = snap.rev
            state.apply_snapshot(snap, client.clock_offset_ms)
            state.apply_queue(client.queue(limit=QUEUE_PAGE))
            if snap.errors_last_id > self._errors_id:
                page = client.errors(since=self._errors_id)
                state.apply_errors(page, client.clock_offset_ms)
                self._errors_id = page.last_id
            if snap.dirs_rev != self._dirs_rev:
                state.apply_dirs(client.dirs())
                self._dirs_rev = snap.dirs_rev
                self._next_io = 0.0
            if snap.config_rev != self._config_rev:
                self._config_rev = snap.config_rev
                self._next_io = 0.0
                self._refresh_config()
            self.first_sync.set()
        if now >= self._next_io:
            io = client.io()
            state.apply_io(io)
            self._next_io = now + IO_EVERY_S
        if now >= self._next_gpu:
            page = client.gpu(since=self._gpu_seq)
            state.apply_gpu(page)
            self._gpu_seq = page.last_seq
            self._next_gpu = now + GPU_EVERY_S

    def _refresh_config(self) -> None:
        view, io = self.client.config(), self.client.io()
        debug = bool(view.sections.get("general", {}).get("debug"))
        self.state.apply_config_lines(build_config_lines(view, io), debug)
