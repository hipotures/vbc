"""Command side of the server: validate, persist, publish domain events.

Every command is absolute and idempotent where possible (see the plan's
"Commands" table). ``put_dirs`` is the port of legacy
``UIManager.on_dirs_apply_changes``: the client sends the whole ordered list,
the server validates it, saves it with ``save_dirs_config`` and publishes
``InputDirsChanged`` + ``RefreshRequested``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import List, Literal, Optional, Sequence

from vbc import contracts as dto
from vbc.config.input_dirs import MAX_INPUT_DIR_LEN, MAX_INPUT_DIRS
from vbc.config.loader import save_dirs_config
from vbc.config.models import AppConfig
from vbc.domain.events import (
    InputDirsChanged,
    InterruptRequested,
    RefreshRequested,
    SetShutdownRequested,
    SetThreadCount,
)
from vbc.infrastructure.event_bus import EventBus
from vbc.service.state_store import StateStore, to_ms


class CommandError(Exception):
    """A rejected command; ``status`` is the HTTP status the API returns."""

    def __init__(self, status: int, title: str, errors: Sequence[str] = (), detail: Optional[str] = None):
        super().__init__(title)
        self.problem = dto.Problem(status=status, title=title, detail=detail, errors=list(errors))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class CommandService:
    def __init__(
        self,
        bus: EventBus,
        store: StateStore,
        config: AppConfig,
        config_path: Optional[Path],
        storage_roots: Optional[List[Path]] = None,
    ) -> None:
        self._bus = bus
        self._store = store
        self._config = config
        self._config_path = config_path
        self._storage_roots = list(storage_roots) if storage_roots else None
        self._dirs_lock = threading.Lock()
        self._dirs: List[dto.DirEntry] = [
            dto.DirEntry(path=str(entry.path), enabled=entry.enabled) for entry in (config.input_dirs or [])
        ]
        self._dirs_rev = store.dirs_rev

    # ── runtime ─────────────────────────────────────────────────────────────

    def set_threads(self, target: int) -> dto.Threads:
        """Absolute target; the orchestrator clamps and reports the effective value."""
        self._bus.publish(SetThreadCount(target=target))
        return self._store.threads

    def refresh(self) -> None:
        """Full rediscovery; also the only way out of paused_on_error / waiting_for_input."""
        self._bus.publish(RefreshRequested())

    def shutdown(self, mode: Literal["graceful", "interrupt"] = "graceful") -> None:
        if mode == "interrupt":
            self._bus.publish(InterruptRequested())
        else:
            self._bus.publish(SetShutdownRequested(requested=True))

    def cancel_shutdown(self) -> None:
        self._bus.publish(SetShutdownRequested(requested=False))

    def set_log_level(self, level: str) -> None:
        logging.getLogger().setLevel(level)
        for handler in logging.getLogger().handlers:
            handler.setLevel(level)

    # ── dirs ────────────────────────────────────────────────────────────────

    def dirs(self) -> dto.DirsState:
        with self._dirs_lock:
            entries = list(self._dirs)
            dirs_rev = self._dirs_rev
        snapshot_rev = self._store.rev
        statuses = []
        for entry in entries:
            path = Path(entry.path)
            statuses.append(dto.DirStatus(
                path=entry.path,
                enabled=entry.enabled,
                exists=path.is_dir(),
                readable=os.access(path, os.R_OK | os.X_OK),
            ))
        return dto.DirsState(rev=snapshot_rev, server_time=to_ms(time.time()), dirs_rev=dirs_rev, entries=statuses)

    def _derived_dirs(self, path: Path) -> List[Path]:
        derived = []
        if self._config.suffix_output_dirs:
            derived.append(path.with_name(f"{path.name}{self._config.suffix_output_dirs}"))
        if self._config.suffix_errors_dirs:
            derived.append(path.with_name(f"{path.name}{self._config.suffix_errors_dirs}"))
        return derived

    def _validate(self, entries: Sequence[dto.DirEntry]) -> List[str]:
        errors: List[str] = []
        if len(entries) > MAX_INPUT_DIRS:
            errors.append(f"too many directories ({len(entries)}); limit is {MAX_INPUT_DIRS}")
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            label = f"entries[{index}] {entry.path!r}"
            raw = entry.path.strip()
            if not raw:
                errors.append(f"{label}: empty path")
                continue
            if len(raw) > MAX_INPUT_DIR_LEN:
                errors.append(f"{label}: longer than {MAX_INPUT_DIR_LEN} characters")
            path = Path(raw)
            if not path.is_absolute():
                errors.append(f"{label}: must be an absolute server path")
            if raw in seen:
                errors.append(f"{label}: duplicate")
            seen.add(raw)
            if self._storage_roots is not None:
                for candidate in [path, *self._derived_dirs(path)]:
                    if not any(_inside(candidate, root) for root in self._storage_roots):
                        errors.append(f"{label}: {candidate} is outside storage.roots")
            if entry.enabled:
                if not path.exists():
                    errors.append(f"{label}: does not exist")
                elif not path.is_dir():
                    errors.append(f"{label}: not a directory")
                elif not os.access(path, os.R_OK | os.X_OK):
                    errors.append(f"{label}: not readable")
        return errors

    def put_dirs(self, entries: Sequence[dto.DirEntry], if_match: Optional[int]) -> dto.DirsState:
        """Replace the whole ordered list; ``if_match`` must equal the current dirs_rev."""
        normalized = [dto.DirEntry(path=e.path.strip(), enabled=e.enabled) for e in entries]
        with self._dirs_lock:
            if if_match is None:
                raise CommandError(428, "If-Match with the current dirs_rev is required")
            if if_match != self._dirs_rev:
                raise CommandError(
                    412, "dirs changed since they were read",
                    detail=f"current dirs_rev is {self._dirs_rev}",
                )
            errors = self._validate(normalized)
            if errors:
                raise CommandError(422, "invalid directory list", errors=errors)
            if self._config_path is not None:
                try:
                    save_dirs_config(self._config_path, [e.model_dump() for e in normalized])
                except OSError as exc:
                    raise CommandError(500, "could not save directories", detail=str(exc)) from exc
            self._dirs = normalized
            self._dirs_rev += 1
            dirs_rev = self._dirs_rev
        self._store.set_dirs_rev(dirs_rev)
        active = [e.path for e in normalized if e.enabled]
        self._bus.publish(InputDirsChanged(active_dirs=active))
        self._bus.publish(RefreshRequested())
        return self.dirs()
