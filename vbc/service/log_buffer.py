"""In-memory ring buffer of application log records for ``GET /api/v1/logs``.

Attached to the root logger next to the stdout (and optional file) handler.
``emit`` only appends under a lock and notifies listeners; no I/O.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional

LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}


@dataclass(frozen=True)
class LogRecordView:
    seq: int
    ts: int  # epoch ms
    level: str
    logger: str
    message: str


class LogBuffer(logging.Handler):
    class Gone(Exception):
        """``since`` is older than the retained buffer (HTTP 410)."""

    def __init__(self, capacity: int = 5000) -> None:
        super().__init__(level=logging.DEBUG)
        self._records: Deque[LogRecordView] = deque(maxlen=capacity)
        self._seq = 0
        self._buffer_lock = threading.Lock()
        self._listeners: List[Callable[[int], None]] = []

    def add_listener(self, callback: Callable[[int], None]) -> None:
        self._listeners.append(callback)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            message = str(record.msg)
        with self._buffer_lock:
            self._seq += 1
            seq = self._seq
            self._records.append(LogRecordView(
                seq=seq, ts=int(record.created * 1000), level=record.levelname,
                logger=record.name, message=message,
            ))
        for callback in list(self._listeners):
            callback(seq)

    @property
    def last_seq(self) -> int:
        with self._buffer_lock:
            return self._seq

    def read(self, since: int = 0, limit: int = 500, level: Optional[str] = None) -> List[LogRecordView]:
        threshold = LEVELS.get((level or "DEBUG").upper(), logging.DEBUG)
        with self._buffer_lock:
            oldest = self._records[0].seq if self._records else self._seq + 1
            if since and since < oldest - 1:
                raise LogBuffer.Gone(f"records up to {oldest - 1} were dropped")
            selected = [
                r for r in self._records
                if r.seq > since and LEVELS.get(r.level, logging.INFO) >= threshold
            ]
        return selected[:limit]
