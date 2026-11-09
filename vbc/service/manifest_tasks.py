"""Failed-manifest maintenance run by the server (it sees the mounts).

restore: put one failed manifest (and its archived sources) back into the
metadata folder; the watcher / next scan queues it again.
repair: recompress supported failures (FFmpeg 244/-12: video without audio)
with a separate engine instance, exactly as scripts/repair_failed_manifests.py;
runs in a background thread, one run at a time.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from vbc import contracts as dto

logger = logging.getLogger(__name__)


class ManifestTasks:
    def __init__(self, server) -> None:  # VbcServer
        self._server = server
        self._lock = threading.Lock()
        self._repair = dto.ManifestRepairStatus(state="idle")
        self._thread: Optional[threading.Thread] = None

    def _checked_path(self, value: str) -> Path:
        from vbc.service.commands import CommandError

        path = Path(value)
        if not path.is_absolute():
            raise CommandError(422, "the path must be absolute (a server path)", errors=[value])
        roots = self._server.current_storage_roots()
        if roots and not any(path == r or r in path.parents for r in roots):
            raise CommandError(422, "the path is outside the storage roots",
                               errors=[value], detail=", ".join(str(r) for r in roots))
        if not path.exists():
            raise CommandError(404, "no such file or folder on the server", errors=[value])
        return path

    def _config_path(self) -> Path:
        from vbc.service.commands import CommandError

        if self._server.config_path is None:
            raise CommandError(409, "this server has no config file")
        return self._server.config_path

    # ── restore ─────────────────────────────────────────────────────────────

    def restore(self, path: str, dry_run: bool) -> dto.ManifestRestoreResult:
        from vbc.maintenance.restore import RestoreError, restore_failed_manifest
        from vbc.service.commands import CommandError

        target = self._checked_path(path)
        try:
            result = restore_failed_manifest(target, self._config_path(), dry_run=dry_run)
        except (RestoreError, OSError, ValueError) as exc:
            raise CommandError(422, "the manifest cannot be restored", detail=str(exc)) from exc
        if not dry_run:
            logger.info(f"MANIFEST_RESTORED: {target} -> {result.manifest_destination} "
                        f"({len(result.restored_sources)} source(s) moved back)")
        return dto.ManifestRestoreResult(
            manifest=str(target), destination=str(result.manifest_destination), dry_run=dry_run,
            restored_sources=[dto.RestoredSource(archive=str(s.archive_path), original=str(s.original_path))
                              for s in result.restored_sources],
            already_present=[str(p) for p in result.already_present_sources],
            error_marker=str(result.error_marker) if result.error_marker else None,
        )

    # ── repair ──────────────────────────────────────────────────────────────

    def repair_status(self) -> dto.ManifestRepairStatus:
        with self._lock:
            return self._repair.model_copy(deep=True)

    def start_repair(self, path: str, dry_run: bool) -> dto.ManifestRepairStatus:
        from vbc.maintenance.repair import RepairError, collect_candidates
        from vbc.service.commands import CommandError

        target = self._checked_path(path)
        config_path = self._config_path()
        try:
            candidates = collect_candidates(target)
        except (RepairError, OSError) as exc:
            raise CommandError(422, "cannot scan for failed manifests", detail=str(exc)) from exc
        with self._lock:
            if self._repair.state == "running":
                raise CommandError(409, "a manifest repair is already running")
            self._repair = dto.ManifestRepairStatus(
                state="running", target=str(target), dry_run=dry_run, total=len(candidates),
                started_at=int(time.time() * 1000),
            )
            self._thread = threading.Thread(target=self._run_repair, args=(candidates, config_path, dry_run),
                                            name="vbc-manifest-repair", daemon=True)
            self._thread.start()
        return self.repair_status()

    def _run_repair(self, candidates, config_path: Path, dry_run: bool) -> None:
        from vbc.maintenance.repair import repair_candidate

        try:
            for candidate in candidates:
                with self._lock:
                    self._repair.current = str(candidate.manifest_path)
                outcome = repair_candidate(candidate, config_path, dry_run=dry_run)
                logger.info(f"MANIFEST_REPAIR {outcome.status}: {outcome.manifest_path} ({outcome.detail})")
                with self._lock:
                    self._repair.done += 1
                    self._repair.outcomes.append(dto.ManifestRepairOutcome(
                        manifest=str(outcome.manifest_path), status=outcome.status, detail=outcome.detail))
            with self._lock:
                self._repair.state = "done"
        except Exception as exc:  # noqa: BLE001 - reported through the status
            logger.exception("Manifest repair failed")
            with self._lock:
                self._repair.state = "failed"
                self._repair.error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._repair.current = None
                self._repair.finished_at = int(time.time() * 1000)
