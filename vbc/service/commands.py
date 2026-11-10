"""Command side of the server: validate, persist, publish domain events.

Every command is absolute and idempotent where possible (see the plan's
"Commands" table). ``put_dirs`` is the port of legacy
``UIManager.on_dirs_apply_changes``: the client sends the whole ordered list,
the server validates it, saves it with ``save_dirs_config`` and publishes
``InputDirsChanged`` + ``RefreshRequested``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence

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


def _changed_keys(old: object, new: object, prefix: str = "") -> List[str]:
    """Dotted keys whose values differ between two parsed YAML documents."""
    if isinstance(old, dict) and isinstance(new, dict):
        keys: List[str] = []
        for key in list(old) + [k for k in new if k not in old]:
            keys += _changed_keys(old.get(key), new.get(key), f"{prefix}{key}.")
        return keys
    return [] if old == new else [prefix.rstrip(".")]


def _validate_config(document: dict) -> AppConfig:
    from pydantic import ValidationError

    try:
        return AppConfig(**document)
    except (ValidationError, TypeError, ValueError) as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()] \
            if isinstance(exc, ValidationError) else [str(exc)]
        raise CommandError(422, "invalid config", errors=errors) from exc


def _model_of(annotation: Any) -> Optional[type]:
    """The BaseModel class behind a field annotation (``X`` or ``Optional[X]``)."""
    from typing import Union, get_args, get_origin
    from pydantic import BaseModel

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) is Union:
        models = [a for a in get_args(annotation) if isinstance(a, type) and issubclass(a, BaseModel)]
        if len(models) == 1:
            return models[0]
    return None


def _check_config_key(key: str) -> List[str]:
    """Errors for a dotted key: it must name a config field, not go below a
    list or free-form mapping, and not be server-owned."""
    parts = key.split(".")
    if not key or any(not p for p in parts):
        return [f"{key!r}: empty key part"]
    if parts[0] in CommandService.SERVER_OWNED or parts[:2] == ["general", "log_path"]:
        return [f"{key}: owned by the server (Dirs list, bind address, storage, log path)"]
    model: Optional[type] = AppConfig
    for depth, part in enumerate(parts):
        if model is None:
            return [f"{key}: {'.'.join(parts[:depth])} is one value (a list or mapping); set it whole"]
        field = model.model_fields.get(part)
        if field is None:
            return [f"{key}: no such setting"]
        model = _model_of(field.annotation)
    return []


def _config_keys(model: type, prefix: str = "") -> List[str]:
    """Dotted keys of every leaf setting (a list or free-form mapping is a leaf)."""
    keys: List[str] = []
    for name, field in model.model_fields.items():
        sub = _model_of(field.annotation)
        keys += _config_keys(sub, f"{prefix}{name}.") if sub else [f"{prefix}{name}"]
    return keys


def _get_path(document: Any, parts: List[str]) -> Any:
    for part in parts:
        if not isinstance(document, dict) or part not in document:
            return None
        document = document[part]
    return document


def _set_path(document: dict, parts: List[str], value: Any, unset: bool) -> None:
    for part in parts[:-1]:
        child = document.get(part)
        if not isinstance(child, dict):
            if unset:
                return
            child = document[part] = {}
        document = child
    if unset:
        document.pop(parts[-1], None)
    else:
        document[parts[-1]] = value


class CommandService:
    def __init__(
        self,
        bus: EventBus,
        store: StateStore,
        config: AppConfig,
        config_path: Optional[Path],
        storage_roots: Optional[StorageRoots] = None,
        dirs_editable: bool = True,
    ) -> None:
        self._bus = bus
        self._dirs_editable = dirs_editable
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

    # ── config ──────────────────────────────────────────────────────────────

    # Sections the server owns: its folder list (Dirs tab), bind address,
    # storage roots. A pushed config never replaces them.
    SERVER_OWNED = ("input_dirs", "disabled_input_dirs", "output_dirs", "errors_dirs",
                    "server", "storage", "web_server")

    def push_config(self, text: str) -> dto.ConfigPushResult:
        """Save a whole vbc.yaml sent by a client; it applies at the next start.

        The server keeps its own sections (SERVER_OWNED) and general.log_path
        (it logs to stdout). The previous file is kept as ``<name>.prev``.
        """
        import yaml
        from pydantic import ValidationError

        if self._config_path is None:
            raise CommandError(409, "this server has no config file")
        try:
            pushed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise CommandError(422, "not valid YAML", detail=str(exc)) from exc
        if not isinstance(pushed, dict):
            raise CommandError(422, "the config must be a YAML mapping")
        with self._dirs_lock:
            current = yaml.safe_load(self._config_path.read_text()) or {}
            merged = {k: v for k, v in pushed.items() if k not in self.SERVER_OWNED}
            kept = []
            for key in self.SERVER_OWNED:
                if key in current:
                    merged[key] = current[key]
                    kept.append(key)
            general = merged.get("general")
            if isinstance(general, dict):
                general = dict(general)
                general["log_path"] = (current.get("general") or {}).get("log_path")
                merged["general"] = general
                kept.append("general.log_path")
            _validate_config(merged)
            changed = _changed_keys(current, merged)
            backup = self._save_config(merged)
        logger.info(f"Config pushed by a client: {len(changed)} change(s); applies at the next start")
        snapshot = self._store.snapshot()
        return dto.ConfigPushResult(
            rev=snapshot.rev, server_time=snapshot.server_time, path=str(self._config_path),
            backup=str(backup), changed=changed, kept_from_server=kept,
        )

    def _save_config(self, document: dict) -> Path:
        """Write ``document`` over the config (temp file + rename); the old file
        is kept as ``<name>.prev``. Caller holds ``_dirs_lock``."""
        import yaml

        assert self._config_path is not None
        backup = self._config_path.with_name(self._config_path.name + ".prev")
        backup.write_text(self._config_path.read_text())
        tmp = self._config_path.with_name(self._config_path.name + ".tmp")
        tmp.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
        os.replace(tmp, self._config_path)
        return backup

    def _read_config_file(self) -> dict:
        import yaml

        if self._config_path is None:
            raise CommandError(409, "this server has no config file")
        try:
            document = yaml.safe_load(self._config_path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise CommandError(500, "could not read the config file", detail=str(exc)) from exc
        if not isinstance(document, dict):
            raise CommandError(500, "the config file is not a YAML mapping")
        return document

    @classmethod
    def _editable_values(cls, document: dict) -> Dict[str, Any]:
        """The file's settings with defaults filled in, without server-owned ones."""
        values = _validate_config(document).model_dump(mode="json")
        for key in cls.SERVER_OWNED:
            values.pop(key, None)
        values.get("general", {}).pop("log_path", None)
        return values

    def config_values(self) -> dto.ConfigValues:
        """Settings as stored in the server's config file (defaults filled in)."""
        with self._dirs_lock:
            values = self._editable_values(self._read_config_file())
        snapshot = self._store.snapshot()
        return dto.ConfigValues(rev=snapshot.rev, server_time=snapshot.server_time,
                                path=str(self._config_path), values=values,
                                keys=[k for k in _config_keys(AppConfig) if not _check_config_key(k)])

    def patch_config(self, changes: Sequence[dto.ConfigChange]) -> dto.ConfigPushResult:
        """Change single settings in the config file, all or none.

        Each change names a dotted key (``general.threads``), the value the
        client last saw (``old``) and the new one. A key whose current value is
        no longer ``old`` was changed by someone else: nothing is written and
        409 lists those keys with the server's value. A list or a free-form
        mapping (``autorotate.patterns``) is one value. ``unset`` removes the
        key from the file (back to the default).
        """
        if not changes:
            raise CommandError(422, "no changes")
        keys = [c.key for c in changes]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise CommandError(422, "a key is changed twice", errors=duplicates)
        with self._dirs_lock:
            current = self._read_config_file()
            values = self._editable_values(current)
            path_errors = [err for c in changes for err in _check_config_key(c.key)]
            if path_errors:
                raise CommandError(422, "unknown or server-owned keys", errors=path_errors)
            conflicts = []
            for change in changes:
                server_value = _get_path(values, change.key.split("."))
                if server_value != change.old:
                    conflicts.append(f"{change.key}: the server has {json.dumps(server_value)}, "
                                     f"not {json.dumps(change.old)}")
            if conflicts:
                raise CommandError(409, "changed meanwhile by another client", errors=conflicts)
            merged = copy.deepcopy(current)
            for change in changes:
                _set_path(merged, change.key.split("."), None if change.unset else change.new, change.unset)
            _validate_config(merged)
            changed = _changed_keys(current, merged)
            backup = self._save_config(merged) if changed else None
        logger.info(f"Config changed by a client: {', '.join(changed) or 'no change'}; applies at the next start")
        snapshot = self._store.snapshot()
        return dto.ConfigPushResult(
            rev=snapshot.rev, server_time=snapshot.server_time, path=str(self._config_path),
            backup=str(backup) if backup else None, changed=changed,
            restart_required=bool(changed),
        )

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

    def _require_editable_dirs(self) -> None:
        if not self._dirs_editable:
            raise CommandError(
                409, "the folder list comes from the command line",
                detail="folders given to `vbc run` / `vbc serve` apply to this run only; the list is read-only and never saved",
            )

    def scan_mounts(self) -> dto.DirsScanResult:
        """Offer every new media mount as a disabled folder (never enabled here:
        a wrong mount must not start compressing). A mount offered once is not
        offered again, even after it was removed from the list."""
        self._require_editable_dirs()
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
        self._require_editable_dirs()
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
        self._bus.publish(RefreshRequested(automatic=True))
        return self.dirs()
