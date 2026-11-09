"""Find (and on request delete) sources that already have a verified output.

A source is a duplicate only when its output in the output folder
(``<input>_out/<relative path>.<ext>``) carries VBC tags naming exactly this
source (``VBCOriginalName`` = file name, ``VBCOriginalSize`` = size in bytes)
and ffprobe reads a video stream with packets from it. Everything else is
reported and left alone. One run at a time; it runs in a background thread
and reports progress through ``status()`` (``/api/v1/sources/cleanup``).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from vbc import contracts as dto

logger = logging.getLogger(__name__)

EXIFTOOL_CONFIG = Path(__file__).resolve().parents[2] / "conf" / "exiftool.conf"
TAG_BATCH = 200
MAX_PROBLEMS = 500


@dataclass
class _Pair:
    source: Path
    output: Path
    size: int


class SourceCleanup:
    def __init__(self, server) -> None:  # VbcServer (no import cycle)
        self._server = server
        self._lock = threading.Lock()
        self._status = dto.SourceCleanupStatus(state="idle")
        self._thread: Optional[threading.Thread] = None

    # ── API ─────────────────────────────────────────────────────────────────

    def status(self) -> dto.SourceCleanupStatus:
        with self._lock:
            return self._status.model_copy(deep=True)

    def start(self, delete: bool, dirs: List[str]) -> dto.SourceCleanupStatus:
        from vbc.service.commands import CommandError

        folders = self._folders(dirs)
        with self._lock:
            if self._status.state == "running":
                raise CommandError(409, "a source cleanup is already running")
            self._status = dto.SourceCleanupStatus(
                state="running", delete=delete, folders=[str(f) for f in folders],
                started_at=int(time.time() * 1000),
            )
            self._thread = threading.Thread(target=self._run, args=(folders, delete),
                                            name="vbc-source-cleanup", daemon=True)
            self._thread.start()
        return self.status()

    # ── work ────────────────────────────────────────────────────────────────

    def _folders(self, dirs: List[str]) -> List[Path]:
        from vbc.service.commands import CommandError

        entries = {Path(e.path): e for e in (self._server.config.input_dirs or [])}
        if dirs:
            chosen = [Path(d.rstrip("/") or "/") for d in dirs]
            unknown = [str(d) for d in chosen if d not in entries]
            if unknown:
                raise CommandError(422, "folders not in the Dirs list", errors=unknown)
            manifest = [str(d) for d in chosen if entries[d].metadata]
            if manifest:
                raise CommandError(422, "manifest folders are not supported", errors=manifest)
            return chosen
        return [p for p, e in entries.items() if e.enabled and not e.metadata]

    def _update(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._status, key, value)

    def _problem(self, path: Path, reason: str) -> None:
        with self._lock:
            self._status.problems_count += 1
            if len(self._status.problems) < MAX_PROBLEMS:
                self._status.problems.append(dto.SourceCleanupProblem(path=str(path), reason=reason))

    def _run(self, folders: List[Path], delete: bool) -> None:
        try:
            pairs = self._collect(folders)
            self._update(total=len(pairs))
            logger.info(f"Source cleanup: {len(pairs)} sources with an output in {len(folders)} folder(s)"
                        f" ({'delete' if delete else 'report only'})")
            for start in range(0, len(pairs), TAG_BATCH):
                batch = pairs[start:start + TAG_BATCH]
                tags = self._read_tags([p.output for p in batch])
                for pair in batch:
                    self._check(pair, tags.get(str(pair.output)), delete)
                self._update(checked=min(start + TAG_BATCH, len(pairs)))
            self._update(state="done", finished_at=int(time.time() * 1000))
            st = self.status()
            logger.info(f"Source cleanup done: {st.duplicates} duplicates ({st.duplicate_bytes} B), "
                        f"{st.deleted} deleted, {st.problems_count} problems")
        except Exception as exc:  # noqa: BLE001 - reported through the status
            logger.exception("Source cleanup failed")
            self._update(state="failed", error=f"{type(exc).__name__}: {exc}",
                         finished_at=int(time.time() * 1000))

    def _collect(self, folders: List[Path]) -> List[_Pair]:
        orch = self._server.orchestrator
        general = self._server.config.general
        extensions = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in general.extensions}
        suffixes: List[str] = []
        for suffix in (orch._output_suffix_for_mode(), ".mp4", ".mkv"):
            if suffix not in suffixes:
                suffixes.append(suffix)
        config = self._server.config
        skip_suffixes = tuple(s for s in (config.suffix_output_dirs, config.suffix_errors_dirs) if s)
        pairs: List[_Pair] = []
        for folder in folders:
            out_root = orch._get_output_dir(folder)
            for root, dirs, files in os.walk(folder):
                root_path = Path(root)
                # Never walk into output/error trees (configured suffixes) nor the
                # output root itself when it lies inside the input folder.
                dirs[:] = sorted(
                    d for d in dirs
                    if not (skip_suffixes and d.endswith(skip_suffixes)) and root_path / d != out_root
                )
                for name in sorted(files):
                    source = root_path / name
                    if source.suffix.lower() not in extensions:
                        continue
                    rel = source.relative_to(folder)
                    output = next((out_root / rel.with_suffix(s) for s in suffixes
                                   if (out_root / rel.with_suffix(s)).is_file()), None)
                    if output is None:
                        continue
                    try:
                        size = source.stat().st_size
                    except OSError:
                        continue
                    pairs.append(_Pair(source, output, size))
        return pairs

    def _read_tags(self, outputs: Iterable[Path]) -> Dict[str, dict]:
        paths = [str(p) for p in outputs]
        if not paths:
            return {}
        result = subprocess.run(
            ["exiftool", "-config", str(EXIFTOOL_CONFIG), "-j", "-n", "-q", "-q",
             "-XMP:VBCOriginalName", "-XMP:VBCOriginalSize", *paths],
            capture_output=True, text=True, timeout=600,
        )
        try:
            entries = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            entries = []
        return {e.get("SourceFile"): e for e in entries if isinstance(e, dict)}

    def _check(self, pair: _Pair, tags: Optional[dict], delete: bool) -> None:
        if not tags or tags.get("VBCOriginalName") in (None, "") or tags.get("VBCOriginalSize") in (None, ""):
            self._problem(pair.source, f"output has no VBC tags: {pair.output}")
            return
        if str(tags["VBCOriginalName"]) != pair.source.name:
            self._problem(pair.source, f"output names another source ({tags['VBCOriginalName']}): {pair.output}")
            return
        try:
            tagged_size = int(str(tags["VBCOriginalSize"]))
        except ValueError:
            self._problem(pair.source, f"output has an unreadable VBCOriginalSize: {pair.output}")
            return
        if tagged_size != pair.size:
            self._problem(pair.source, f"size differs from the tagged original ({tagged_size} B): {pair.output}")
            return
        ok, why = self._probe(pair.output)
        if not ok:
            self._problem(pair.source, f"output fails ffprobe ({why}): {pair.output}")
            return
        with self._lock:
            self._status.duplicates += 1
            self._status.duplicate_bytes += pair.size
        if delete:
            self._delete(pair)

    def _probe(self, output: Path) -> Tuple[bool, str]:
        try:
            info = self._server.orchestrator._get_output_info(output)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        if not info.get("has_video_stream"):
            return False, "no video stream"
        if int(info.get("video_packets") or 0) <= 0:
            return False, "no video packets"
        return True, ""

    def _delete(self, pair: _Pair) -> None:
        try:
            if pair.source.samefile(pair.output):
                self._problem(pair.source, "source and output are the same file; not deleted")
                return
            if pair.source.stat().st_size != pair.size:
                self._problem(pair.source, "source changed during the check; not deleted")
                return
            pair.source.unlink()
        except OSError as exc:
            self._problem(pair.source, f"delete failed: {exc}")
            return
        logger.info(f"DUPLICATE_SOURCE_DELETED: source={pair.source} size_bytes={pair.size} output={pair.output}")
        with self._lock:
            self._status.deleted += 1
            self._status.deleted_bytes += pair.size
