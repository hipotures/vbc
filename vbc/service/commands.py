"""Command side of the server: validate, persist, publish domain events.

Every command is absolute and idempotent where possible (see the plan's
"Commands" table). ``put_dirs`` is the port of legacy
``UIManager.on_dirs_apply_changes``: the client sends the whole ordered list,
the server validates it, saves it with ``save_dirs_config`` and publishes
``InputDirsChanged`` + ``RefreshRequested``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import List, Literal, Optional, Sequence

from vbc import contracts as dto
from vbc.config.input_dirs import MAX_INPUT_DIR_LEN, MAX_INPUT_DIRS
from vbc.config.loader import save_dirs_config
from vbc.config.models import AppConfig, InputDirEntry
from vbc.domain.events import (
    InputDirsChanged,
    InterruptRequested,
    RefreshRequested,
    SetShutdownRequested,
    SetThreadCount,
)
from vbc.infrastructure.event_bus import EventBus
from vbc.service.state_store import StateStore, to_ms
from vbc.service.storage import StorageRoots, is_network_fs, media_mounts

logger = logging.getLogger(__name__)

# Polling interval the server picks for a watched folder on a network file system.
NETWORK_POLL_INTERVAL_S = 10.0


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


def is_manifest_dir(path: Path, sample: int = 20) -> bool:
    """Does the folder hold VBC manifests (JSON with ``inputs`` and ``output_path``)?"""
    try:
        candidates = sorted(path.glob("*.json"))[:sample]
    except OSError:
        return False
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "inputs" in data and "output_path" in data:
            return True
    return False


def _has_videos(path: Path, extensions: set, max_dirs: int = 50) -> bool:
    """A video directly in ``path`` or in one of its first ``max_dirs`` subfolders."""
    def direct(folder: Path) -> tuple[bool, List[Path]]:
        subdirs = []
        try:
            for item in folder.iterdir():
                if item.suffix.lower().lstrip(".") in extensions and item.is_file():
                    return True, []
                if len(subdirs) < max_dirs and item.is_dir():
                    subdirs.append(item)
        except OSError:
            pass
        return False, subdirs

    hit, subdirs = direct(path)
    return hit or any(direct(sub)[0] for sub in subdirs)


def _to_dto(entry: InputDirEntry) -> dto.DirEntry:
    return dto.DirEntry(
        path=str(entry.path), enabled=entry.enabled, metadata=entry.metadata, watch=entry.watch,
        watch_mode=entry.watch_mode, poll_interval_seconds=entry.poll_interval_seconds,
    )


class CommandService:
    def __init__(
        self,
        bus: EventBus,
        store: StateStore,
        config: AppConfig,
        config_path: Optional[Path],
        storage_roots: Optional[StorageRoots] = None,
    ) -> None:
        self._bus = bus
        self._store = store
        self._config = config
        self._config_path = config_path
        self._storage_roots = storage_roots
        self._dirs_lock = threading.Lock()
        self._dirs: List[dto.DirEntry] = [_to_dto(entry) for entry in (config.input_dirs or [])]
        self._dirs_rev = store.dirs_rev

    # ── runtime ─────────────────────────────────────────────────────────────

    def set_threads(self, target: int) -> dto.Threads:
        """Absolute target; the orchestrator clamps and reports the effective value."""
        self._bus.publish(SetThreadCount(target=target))
        return self._store.threads

    def refresh(self) -> None:
        """Full rediscovery; also the only way out of paused_on_error / waiting_for_input."""
        self._bus.publish(RefreshRequested())

    def stop(self, mode: Literal["graceful", "interrupt"] = "graceful") -> None:
        """Stop processing; the server keeps running and a refresh resumes.

        graceful: running jobs finish, no new ones start. interrupt: running
        jobs are stopped now.
        """
        if mode == "interrupt":
            self._bus.publish(InterruptRequested())
        else:
            self._bus.publish(SetShutdownRequested(requested=True))

    def cancel_stop(self) -> None:
        """Withdraw a pending graceful stop (running jobs continue, the queue too)."""
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
                **entry.model_dump(),
                exists=path.is_dir(),
                readable=os.access(path, os.R_OK | os.X_OK),
            ))
        return dto.DirsState(rev=snapshot_rev, server_time=to_ms(time.time()), dirs_rev=dirs_rev, entries=statuses)

    @staticmethod
    def _complete(entry: dto.DirEntry, saved: Optional[InputDirEntry]) -> dto.DirEntry:
        """Fill the fields a request left out: the saved entry of a listed folder,
        else what the folder shows (manifests inside, network file system)."""
        path = entry.path.strip()
        metadata = entry.metadata
        if metadata is None:
            metadata = saved.metadata if saved else is_manifest_dir(Path(path))
        watch = entry.watch
        if watch is None:
            watch = saved.watch if saved else metadata
        watch_mode = entry.watch_mode or (saved.watch_mode if saved and saved.watch else None)
        interval = entry.poll_interval_seconds or (saved.poll_interval_seconds if saved and saved.watch else None)
        if watch_mode is None:
            watch_mode = "polling" if is_network_fs(Path(path)) else "inotify"
        if interval is None:
            interval = NETWORK_POLL_INTERVAL_S if watch_mode == "polling" else InputDirEntry.model_fields[
                "poll_interval_seconds"].default
        return dto.DirEntry(
            path=path, enabled=entry.enabled, metadata=metadata, watch=watch,
            watch_mode=watch_mode, poll_interval_seconds=interval,
        )

    def _derived_dirs(self, path: Path) -> List[Path]:
        derived = []
        if self._config.suffix_output_dirs:
            derived.append(path.with_name(f"{path.name}{self._config.suffix_output_dirs}"))
        if self._config.suffix_errors_dirs:
            derived.append(path.with_name(f"{path.name}{self._config.suffix_errors_dirs}"))
        return derived

    def _validate(self, entries: Sequence[dto.DirEntry]) -> List[str]:
        errors: List[str] = []
        roots = self._storage_roots.current() if self._storage_roots is not None else None
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
            if roots is not None:
                for candidate in [path, *self._derived_dirs(path)]:
                    if not any(_inside(candidate, root) for root in roots):
                        allowed = ", ".join(str(root) for root in roots) or "none (no media mounts)"
                        errors.append(f"{label}: {candidate} is outside the storage roots ({allowed})")
            if entry.enabled:
                if not path.exists():
                    errors.append(f"{label}: does not exist")
                elif not path.is_dir():
                    errors.append(f"{label}: not a directory")
                elif not os.access(path, os.R_OK | os.X_OK):
                    errors.append(f"{label}: not readable")
        return errors

    def scan_mounts(self) -> dto.DirsScanResult:
        """Offer every new media mount as a disabled folder (never enabled here:
        a wrong mount must not start compressing). A mount offered once is not
        offered again, even after it was removed from the list."""
        added: List[str] = []
        roots = self._storage_roots
        if roots is not None and roots.detect_mounts:
            with self._dirs_lock:
                discovered = list(self._config.storage.discovered)
                listed = {Path(e.path) for e in self._dirs}
                new_dirs = list(self._dirs)
                for candidate in self._mount_candidates(roots.exclude):
                    if str(candidate) in discovered or candidate in listed:
                        continue
                    if len(new_dirs) >= MAX_INPUT_DIRS:  # offered by a later scan, once there is room
                        logger.warning(f"{candidate} not offered: the list already has {MAX_INPUT_DIRS} folders")
                        continue
                    discovered.append(str(candidate))
                    new_dirs.append(self._complete(dto.DirEntry(path=str(candidate), enabled=False), None))
                    added.append(str(candidate))
                if discovered != self._config.storage.discovered:
                    self._commit(new_dirs, discovered)
            if added:
                logger.info(f"Mounts offered in Dirs (disabled): {', '.join(added)}")
                self._after_commit()
        state = self.dirs()
        return dto.DirsScanResult(**state.model_dump(), added=added)

    def _mount_candidates(self, exclude: List[Path]) -> List[Path]:
        """Folders directly inside each media mount that hold manifests or videos.

        Not the mount itself: outputs go to ``<folder>_out`` beside the folder,
        which for a mount point would lie outside the mount (on the volume).
        """
        suffixes = tuple(s for s in (self._config.suffix_output_dirs, self._config.suffix_errors_dirs) if s)
        extensions = {e.lower().lstrip(".") for e in self._config.general.extensions}
        found: List[Path] = []
        for mount in media_mounts(exclude):
            try:
                children = sorted(p for p in mount.iterdir() if p.is_dir())
            except OSError:
                continue
            for child in children:
                if child.name.startswith(".") or (suffixes and child.name.endswith(suffixes)):
                    continue
                if is_manifest_dir(child) or _has_videos(child, extensions):
                    found.append(child)
        return found

    def _commit(self, normalized: List[dto.DirEntry], discovered: Optional[List[str]] = None) -> None:
        """Save and apply a validated list (caller holds ``_dirs_lock``)."""
        saved = {Path(e.path): e for e in (self._config.input_dirs or [])}
        config_entries = [
            InputDirEntry(**{**(saved[Path(e.path)].model_dump() if Path(e.path) in saved else {}),
                             **e.model_dump()})
            for e in normalized
        ]
        if self._config_path is not None:
            try:
                save_dirs_config(self._config_path, [e.model_dump() for e in normalized], discovered)
            except OSError as exc:
                raise CommandError(500, "could not save directories", detail=str(exc)) from exc
        # Running components (orchestrator, watchers) read the entries on InputDirsChanged.
        self._config.input_dirs = config_entries
        if discovered is not None:
            self._config.storage.discovered = discovered
        self._dirs = normalized
        self._dirs_rev += 1
        self._store.set_dirs_rev(self._dirs_rev)

    def _after_commit(self) -> None:
        with self._dirs_lock:
            active = [e.path for e in self._dirs if e.enabled]
        self._bus.publish(InputDirsChanged(active_dirs=active))

    def put_dirs(self, entries: Sequence[dto.DirEntry], if_match: Optional[int]) -> dto.DirsState:
        """Replace the whole ordered list; ``if_match`` must equal the current dirs_rev."""
        with self._dirs_lock:
            saved = {Path(e.path): e for e in (self._config.input_dirs or [])}
            normalized = [self._complete(e, saved.get(Path(e.path.strip()))) for e in entries]
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
            self._commit(normalized)
        self._after_commit()
        self._bus.publish(RefreshRequested())
        return self.dirs()
