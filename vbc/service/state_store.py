"""Server read model: domain events in, versioned snapshots out.

Business half of legacy ``UIState`` + ``UIManager`` (vbc_legacy/ui/), without
any view state. Bus callbacks arrive synchronously from orchestrator worker
threads and from every parsed FFmpeg progress line, so they only mutate memory
under one lock, bump ``rev`` and notify listeners; no I/O, and ``publish()`` is
never called while the lock is held.

Counters follow dead reckoning (docs/architecture/service-split.md, "Changing
counters without streaming every tick"): progress events bump ``rev`` only when
a client's extrapolation from the last published anchor would be wrong.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

from vbc import contracts as dto
from vbc.config.models import AppConfig
from vbc.domain.events import (
    ActionMessage,
    DiscoveryFinished,
    DiscoveryStarted,
    HardwareCapabilityExceeded,
    InterruptRequested,
    JobCompleted,
    JobFailed,
    JobProgressUpdated,
    JobStarted,
    ProcessingFinished,
    ProcessingPausedOnError,
    QueueUpdated,
    RefreshFinished,
    RepairFinished,
    RepairStarted,
    SetShutdownRequested,
    ThreadCountChanged,
    WaitingForInput,
)
from vbc.domain.models import CompressionJob, JobStatus
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.gpu_monitor import GpuSample
from vbc.version import vbc_version

# Dead-reckoning thresholds
PROGRESS_DRIFT_PCT = 2.0
RATE_HORIZON_S = 10.0  # a rate change matters if it would drift > PROGRESS_DRIFT_PCT within this horizon
ANCHOR_MAX_AGE_S = 30.0
RATE_EWMA_ALPHA = 0.3

THROUGHPUT_WINDOW_S = 30.0
THROUGHPUT_HISTORY_S = 60.0
RECENT_JOBS_KEPT = 500
RECENT_JOBS_IN_SNAPSHOT = 20
ERRORS_KEPT = 1000
GPU_SAMPLES_KEPT = 600

_EXIT_CODE_RE = re.compile(r"exited with code (-?\d+)")


@dataclass
class _Anchor:
    percent: float
    at: float
    rate: float


@dataclass
class _ActiveJob:
    job: CompressionJob
    started_at: float
    last_percent: Optional[float] = None
    last_at: Optional[float] = None
    rate: float = 0.0
    anchor: Optional[_Anchor] = None


@dataclass
class _Error:
    id: int
    created_at: float
    path: Path
    error_message: str
    job_id: Optional[str] = None
    stage: Optional[str] = None
    exit_code: Optional[int] = None
    manifest_request_id: Optional[str] = None
    size_bytes: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    codec: Optional[str] = None
    audio_codec: Optional[str] = None
    duration_s: Optional[float] = None


@dataclass
class _State:
    counters: dto.Counters = field(default_factory=dto.Counters)
    discovery: dto.Discovery = field(default_factory=dto.Discovery)
    total_input_bytes: int = 0
    total_output_bytes: int = 0
    throughput_history: Deque[Tuple[float, int]] = field(default_factory=deque)
    processing_started_at: Optional[float] = None
    active: Dict[str, _ActiveJob] = field(default_factory=dict)
    recent: Deque[dto.RecentJob] = field(default_factory=lambda: deque(maxlen=RECENT_JOBS_KEPT))
    pending: List = field(default_factory=list)
    stop_requested: bool = False
    interrupt_requested: bool = False
    stopped: bool = False
    exiting: bool = False
    error_paused: bool = False
    pause_reason: Optional[str] = None
    status_message: Optional[str] = None
    repair: dto.Repair = field(default_factory=dto.Repair)
    finished: bool = False
    waiting_for_input: bool = False
    last_action: Optional[Tuple[str, float]] = None


def _media(job: CompressionJob) -> Optional[dto.MediaInfo]:
    meta = job.source_file.metadata
    if meta is None:
        return None
    return dto.MediaInfo(
        width=meta.width, height=meta.height, fps=meta.fps, duration_s=meta.duration,
        codec=meta.codec, audio_codec=meta.audio_codec, camera_model=meta.camera_model,
        custom_cq=meta.custom_cq,
    )


def _display_fields(job: CompressionJob) -> dict:
    source = job.config_source
    return {
        "quality": job.quality_display,
        "quality_value": job.quality_value,
        "rotation_angle": job.rotation_angle,
        "config_source": source.value if hasattr(source, "value") else source,
        "media": _media(job),
    }


def to_ms(seconds: Optional[float]) -> Optional[int]:
    """Epoch seconds -> epoch milliseconds (API time unit)."""
    return None if seconds is None else int(round(seconds * 1000))


class StateStore:
    """Thread-safe read model fed by the EventBus."""

    def __init__(
        self,
        bus: EventBus,
        config: AppConfig,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._config = config
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._listeners: List[Callable[[int], None]] = []
        self._rev = 1
        self._config_rev = 1
        self._dirs_rev = 1
        self._encoder: Optional[str] = None
        self._encoder_note: Optional[str] = None
        self._s = _State()
        general = config.general
        maximum = max(general.max_threads, general.threads)
        self._threads = dto.Threads(current=max(1, min(maximum, general.threads)), minimum=1, maximum=maximum)
        self._errors: Deque[_Error] = deque(maxlen=ERRORS_KEPT)
        self._error_seq = 0
        self._discovery_error_keys: set[Tuple[Path, str]] = set()
        self._gpu: Deque[dto.GpuSample] = deque(maxlen=GPU_SAMPLES_KEPT)
        self._gpu_seq = 0
        self._failure: Optional[Tuple[dto.RunStatus, str]] = None
        self._subscribe(bus)

    # ── wiring ──────────────────────────────────────────────────────────────

    def _subscribe(self, bus: EventBus) -> None:
        handlers = {
            DiscoveryStarted: self._on_discovery_started,
            DiscoveryFinished: self._on_discovery_finished,
            RefreshFinished: self._on_refresh_finished,
            ThreadCountChanged: self._on_thread_count_changed,
            SetShutdownRequested: self._on_shutdown_requested,
            InterruptRequested: self._on_interrupt_requested,
            JobStarted: self._on_job_started,
            JobCompleted: self._on_job_completed,
            JobFailed: self._on_job_failed,
            JobProgressUpdated: self._on_job_progress,
            HardwareCapabilityExceeded: self._on_hw_cap,
            QueueUpdated: self._on_queue_updated,
            ActionMessage: self._on_action_message,
            ProcessingFinished: self._on_processing_finished,
            ProcessingPausedOnError: self._on_paused_on_error,
            RepairStarted: self._on_repair_started,
            RepairFinished: self._on_repair_finished,
            WaitingForInput: self._on_waiting_for_input,
        }
        for event_type, handler in handlers.items():
            bus.subscribe(event_type, handler)

    def add_listener(self, callback: Callable[[int], None]) -> None:
        """Called (outside the lock) with the new rev after every change."""
        with self._lock:
            self._listeners.append(callback)

    def _changed_unlocked(self) -> int:
        self._rev += 1
        self._changed.notify_all()
        return self._rev

    def _notify(self, rev: Optional[int]) -> None:
        if rev is None:
            return
        for callback in list(self._listeners):
            callback(rev)

    def _mutate(self, fn: Callable[[_State, float], bool]) -> None:
        """Run ``fn(state, now)`` under the lock; bump rev when it returns True."""
        with self._lock:
            now = self._clock()
            rev = self._changed_unlocked() if fn(self._s, now) else None
        self._notify(rev)

    @property
    def rev(self) -> int:
        with self._lock:
            return self._rev

    def wait_for_change(self, since: int, timeout: float) -> int:
        """Block until ``rev > since`` or timeout; returns the current rev."""
        with self._changed:
            self._changed.wait_for(lambda: self._rev > since, timeout=timeout)
            return self._rev

    # ── event handlers (port of legacy UIManager business handlers) ─────────

    def _on_discovery_started(self, event: DiscoveryStarted) -> None:
        def fn(s: _State, now: float) -> bool:
            s.discovery.finished = False
            s.waiting_for_input = False
            s.finished = False
            s.error_paused = False
            s.pause_reason = None
            s.status_message = None
            return True

        self._mutate(fn)

    def _on_discovery_finished(self, event: DiscoveryFinished) -> None:
        def fn(s: _State, now: float) -> bool:
            s.discovery = dto.Discovery(
                finished=True,
                finished_at=to_ms(now),
                files_found=event.files_found,
                files_to_process=event.files_to_process,
                already_compressed=event.already_compressed,
                ignored_small=event.ignored_small,
                ignored_err=event.ignored_err,
                source_folders_count=event.source_folders_count,
            )
            s.counters.ignored_av1 = event.ignored_av1
            s.counters.completed_at_last_discovery = s.counters.completed
            s.counters.failed_at_last_discovery = s.counters.failed
            for entry in event.ignored_err_entries:
                message = (entry.error_message or "Unknown error").strip() or "Unknown error"
                key = (entry.path, message)
                if key in self._discovery_error_keys:
                    continue
                self._discovery_error_keys.add(key)
                self._add_error_unlocked(
                    now, path=entry.path, error_message=message,
                    size_bytes=entry.size_bytes, stage="discovery",
                )
            return True

        self._mutate(fn)

    def _on_refresh_finished(self, event: RefreshFinished) -> None:
        if event.added <= 0 and event.removed <= 0:
            return

        def fn(s: _State, now: float) -> bool:
            s.counters.session_completed_base = s.counters.completed
            return True

        self._mutate(fn)

    def _on_thread_count_changed(self, event: ThreadCountChanged) -> None:
        def fn(s: _State, now: float) -> bool:
            new = dto.Threads(current=event.current, minimum=event.minimum, maximum=event.maximum)
            if new == self._threads:
                return False
            self._threads = new
            return True

        self._mutate(fn)

    def _on_shutdown_requested(self, event: SetShutdownRequested) -> None:
        def fn(s: _State, now: float) -> bool:
            if s.stop_requested == event.requested:
                return False
            s.stop_requested = event.requested
            return True

        self._mutate(fn)

    def _on_interrupt_requested(self, event: InterruptRequested) -> None:
        def fn(s: _State, now: float) -> bool:
            if s.interrupt_requested:
                return False
            s.interrupt_requested = True
            return True

        self._mutate(fn)

    def _on_job_started(self, event: JobStarted) -> None:
        job = event.job

        def fn(s: _State, now: float) -> bool:
            s.waiting_for_input = False
            s.finished = False
            if s.processing_started_at is None:
                s.processing_started_at = now
            current = s.active.get(job.job_id)
            if current is None:
                s.active[job.job_id] = _ActiveJob(job=job, started_at=now)
            else:
                if job.status == JobStatus.PROCESSING and current.job.status != JobStatus.PROCESSING:
                    current.started_at = now  # encoding starts after preflight
                current.job = job
            return True

        self._mutate(fn)

    def _finish_unlocked(self, s: _State, now: float, job: CompressionJob) -> Optional[float]:
        active = s.active.pop(job.job_id, None)
        return None if active is None else now - active.started_at

    def _record_throughput_unlocked(self, s: _State, now: float, size: int) -> None:
        s.throughput_history.append((now, size))
        cutoff = now - THROUGHPUT_HISTORY_S
        while s.throughput_history and s.throughput_history[0][0] < cutoff:
            s.throughput_history.popleft()

    @staticmethod
    def _recent(job: CompressionJob, now: float, duration: Optional[float], output_bytes: Optional[int],
                error: Optional[str]) -> dto.RecentJob:
        return dto.RecentJob(
            id=job.job_id,
            name=job.source_file.path.name,
            path=str(job.source_file.path),
            status=job.status.value if hasattr(job.status, "value") else str(job.status),
            finished_at=to_ms(now),
            input_bytes=job.source_file.size_bytes,
            output_bytes=output_bytes,
            duration_s=duration,
            error_message=error,
            warning=getattr(job, "warning", None),
            **_display_fields(job),
        )

    def _on_job_completed(self, event: JobCompleted) -> None:
        job = event.job
        output_size = job.output_size_bytes
        if output_size is None:
            # Legacy fallback: read the output size (one stat, outside the lock).
            try:
                output_size = job.output_path.stat().st_size if job.output_path else 0
            except OSError:
                output_size = 0

        def fn(s: _State, now: float) -> bool:
            duration = self._finish_unlocked(s, now, job)
            if job.error_message and "kept original" in job.error_message:
                s.counters.min_ratio_skip += 1
            s.counters.completed += 1
            s.counters.sources_deleted += getattr(job, "sources_deleted", 0) or 0
            if getattr(job, "warning", None):
                s.counters.warnings += 1
            s.total_input_bytes += job.source_file.size_bytes
            s.total_output_bytes += output_size
            self._record_throughput_unlocked(s, now, job.source_file.size_bytes)
            s.recent.appendleft(self._recent(job, now, duration, output_size, job.error_message))
            return True

        self._mutate(fn)

    def _on_job_failed(self, event: JobFailed) -> None:
        job = event.job
        message = event.error_message

        def fn(s: _State, now: float) -> bool:
            duration = self._finish_unlocked(s, now, job)
            if job.status == JobStatus.SKIPPED and message == "Manifest ignored during preflight":
                s.counters.skipped += 1
                return True
            self._add_job_error_unlocked(now, job, message)
            if message and "Already encoded in AV1" in message:
                s.counters.ignored_av1 += 1
            elif message and "Camera model" in message:
                s.counters.cam_skipped += 1
            elif job.status == JobStatus.INTERRUPTED:
                s.counters.interrupted += 1
                s.recent.appendleft(self._recent(job, now, duration, None, message))
            else:
                s.counters.failed += 1
                self._record_throughput_unlocked(s, now, 0)
                s.recent.appendleft(self._recent(job, now, duration, None, message))
            return True

        self._mutate(fn)

    def _on_hw_cap(self, event: HardwareCapabilityExceeded) -> None:
        def fn(s: _State, now: float) -> bool:
            s.counters.hw_cap += 1
            s.active.pop(event.job.job_id, None)
            return True

        self._mutate(fn)

    def _on_job_progress(self, event: JobProgressUpdated) -> None:
        job_id = event.job.job_id
        percent = float(event.progress_percent)

        def fn(s: _State, now: float) -> bool:
            active = s.active.get(job_id)
            if active is None:
                return False
            if active.last_percent is not None and active.last_at is not None and now > active.last_at:
                sample = (percent - active.last_percent) / (now - active.last_at)
                if sample >= 0:
                    active.rate = (
                        sample if active.rate == 0.0
                        else RATE_EWMA_ALPHA * sample + (1 - RATE_EWMA_ALPHA) * active.rate
                    )
            active.last_percent, active.last_at = percent, now
            return self._maybe_reanchor(active, percent, now)

        self._mutate(fn)

    @staticmethod
    def _maybe_reanchor(active: _ActiveJob, percent: float, now: float) -> bool:
        anchor = active.anchor
        if anchor is None:
            active.anchor = _Anchor(percent, now, active.rate)
            return True
        predicted = min(anchor.percent + anchor.rate * (now - anchor.at), 99.5)
        rate_changed = (
            percent < 99.5
            and abs(active.rate - anchor.rate) * RATE_HORIZON_S > PROGRESS_DRIFT_PCT
        )
        if (
            abs(predicted - percent) > PROGRESS_DRIFT_PCT
            or rate_changed
            or now - anchor.at >= ANCHOR_MAX_AGE_S
        ):
            active.anchor = _Anchor(percent, now, active.rate)
            return True
        return False

    def _on_queue_updated(self, event: QueueUpdated) -> None:
        pending = list(event.pending_files)

        def fn(s: _State, now: float) -> bool:
            s.pending = pending
            return True

        self._mutate(fn)

    def _on_action_message(self, event: ActionMessage) -> None:
        def fn(s: _State, now: float) -> bool:
            s.last_action = (event.message, now)
            return True

        self._mutate(fn)

    def _on_processing_finished(self, event: ProcessingFinished) -> None:
        def fn(s: _State, now: float) -> bool:
            s.finished = True
            s.waiting_for_input = False
            s.error_paused = False
            s.pause_reason = None
            s.status_message = None
            s.repair.active = False
            return True

        self._mutate(fn)

    def _on_paused_on_error(self, event: ProcessingPausedOnError) -> None:
        def fn(s: _State, now: float) -> bool:
            s.finished = False
            s.waiting_for_input = True
            s.error_paused = True
            s.pause_reason = event.reason
            s.status_message = event.message
            s.repair.active = False
            return True

        self._mutate(fn)

    def _on_repair_started(self, event: RepairStarted) -> None:
        def fn(s: _State, now: float) -> bool:
            s.finished = False
            s.waiting_for_input = False
            s.error_paused = False
            s.pause_reason = None
            s.status_message = None
            s.repair = dto.Repair(active=True, candidates=event.candidate_count, repaired=0)
            return True

        self._mutate(fn)

    def _on_repair_finished(self, event: RepairFinished) -> None:
        def fn(s: _State, now: float) -> bool:
            s.repair = dto.Repair(active=False, candidates=event.attempted, repaired=event.repaired)
            return True

        self._mutate(fn)

    def _on_waiting_for_input(self, event: WaitingForInput) -> None:
        def fn(s: _State, now: float) -> bool:
            s.finished = False
            s.waiting_for_input = True
            s.repair.active = False
            return True

        self._mutate(fn)

    # ── errors ──────────────────────────────────────────────────────────────

    def _add_error_unlocked(self, now: float, **fields) -> None:
        self._error_seq += 1
        self._errors.append(_Error(id=self._error_seq, created_at=now, **fields))

    def _add_job_error_unlocked(self, now: float, job: CompressionJob, message: str) -> None:
        source = job.source_file
        meta = source.metadata
        text = (message or job.error_message or "Unknown error").strip() or "Unknown error"
        match = _EXIT_CODE_RE.search(text)
        request = source.metadata_request
        self._add_error_unlocked(
            now,
            path=source.path,
            error_message=text,
            job_id=job.job_id,
            exit_code=int(match.group(1)) if match else None,
            manifest_request_id=request.manifest.request_id if request is not None else None,
            size_bytes=source.size_bytes,
            width=meta.width if meta else None,
            height=meta.height if meta else None,
            fps=meta.fps if meta else None,
            codec=meta.codec if meta else None,
            audio_codec=meta.audio_codec if meta else None,
            duration_s=meta.duration if meta else None,
        )

    class ErrorsGone(Exception):
        """``since`` is older than the retained buffer (HTTP 410)."""

    def errors(self, since: int = 0, limit: int = 200) -> dto.ErrorsPage:
        with self._lock:
            oldest = self._errors[0].id if self._errors else self._error_seq + 1
            if since and since < oldest - 1:
                raise StateStore.ErrorsGone(f"entries up to {oldest - 1} were dropped")
            entries = [e for e in self._errors if e.id > since][:limit]
            return dto.ErrorsPage(
                rev=self._rev, server_time=to_ms(self._clock()), last_id=self._error_seq,
                entries=[
                    dto.ErrorEntry(
                        id=e.id, created_at=to_ms(e.created_at), path=str(e.path), job_id=e.job_id,
                        stage=e.stage, error_message=e.error_message, exit_code=e.exit_code,
                        manifest_request_id=e.manifest_request_id, size_bytes=e.size_bytes,
                        width=e.width, height=e.height, fps=e.fps, codec=e.codec,
                        audio_codec=e.audio_codec, duration_s=e.duration_s,
                    )
                    for e in entries
                ],
            )

    # ── GPU (GpuSink) ───────────────────────────────────────────────────────

    def add_gpu_sample(self, sample: GpuSample) -> None:
        """GpuSink: keep a bounded history; does not bump the main rev."""
        with self._lock:
            self._gpu_seq += 1
            self._gpu.append(dto.GpuSample(
                seq=self._gpu_seq, at=to_ms(self._clock()), available=sample.raw is not None,
                temp_c=sample.temp_c, power_w=sample.power_w, gpu_util_pct=sample.gpu_util_pct,
                mem_util_pct=sample.mem_util_pct, fan_pct=sample.fan_pct, raw=sample.raw,
                enc_util_pct=getattr(sample, "enc_util_pct", None),
                dec_util_pct=getattr(sample, "dec_util_pct", None),
                mem_used_mib=getattr(sample, "mem_used_mib", None),
                mem_total_mib=getattr(sample, "mem_total_mib", None),
                power_limit_w=getattr(sample, "power_limit_w", None),
                gpu_clock_mhz=getattr(sample, "gpu_clock_mhz", None),
                mem_clock_mhz=getattr(sample, "mem_clock_mhz", None),
                throttle=getattr(sample, "throttle", None),
            ))

    def gpu(self, since: int = 0, enabled: bool = True) -> dto.GpuPage:
        with self._lock:
            return dto.GpuPage(
                rev=self._rev, server_time=to_ms(self._clock()), enabled=enabled,
                last_seq=self._gpu_seq, samples=[g for g in self._gpu if g.seq > since],
            )

    def set_failure(self, status: dto.RunStatus, message: str) -> None:
        """The orchestrator thread ended abnormally (verification abort, crash)."""
        def fn(st: _State, now: float) -> bool:
            self._failure = (status, message)
            st.status_message = message
            return True

        self._mutate(fn)

    def set_stopped(self) -> None:
        """The orchestrator returned after a stop; the server waits for a refresh."""
        def fn(st: _State, now: float) -> bool:
            st.stopped = True
            st.active.clear()
            return True

        self._mutate(fn)

    def mark_resumed(self) -> None:
        """A refresh restarts processing after a stop."""
        def fn(st: _State, now: float) -> bool:
            st.stopped = False
            st.stop_requested = False
            st.interrupt_requested = False
            st.waiting_for_input = False
            return True

        self._mutate(fn)

    def set_exiting(self) -> None:
        """The server process is exiting (signal)."""
        def fn(st: _State, now: float) -> bool:
            st.exiting = True
            return True

        self._mutate(fn)

    def set_idle(self) -> None:
        """A pass ended without a following wait (demo mode)."""
        def fn(st: _State, now: float) -> bool:
            st.waiting_for_input = True
            return True

        self._mutate(fn)

    # ── revisions owned by other services ───────────────────────────────────

    def bump_config_rev(self) -> None:
        with self._lock:
            self._config_rev += 1
            rev = self._changed_unlocked()
        self._notify(rev)

    def set_encoder(self, encoder: str, note: Optional[str] = None) -> None:
        with self._lock:
            self._encoder, self._encoder_note = encoder, note
            rev = self._changed_unlocked()
        self._notify(rev)

    def set_dirs_rev(self, dirs_rev: int) -> None:
        with self._lock:
            if dirs_rev == self._dirs_rev:
                return
            self._dirs_rev = dirs_rev
            rev = self._changed_unlocked()
        self._notify(rev)

    @property
    def dirs_rev(self) -> int:
        with self._lock:
            return self._dirs_rev

    @property
    def threads(self) -> dto.Threads:
        with self._lock:
            return self._threads

    # ── snapshots ───────────────────────────────────────────────────────────

    def _source_deletion(self) -> bool:
        """Whether any policy in force can delete sources (manifest policy None
        means each manifest decides, so deletion is possible)."""
        cfg = self._config
        if cfg is None:
            return True
        meta = getattr(cfg.metadata, "source_policy", None)
        return cfg.general.source_policy == "delete_after_success" or meta in (None, "delete_after_success")

    def _run_status_unlocked(self) -> dto.RunStatus:
        s = self._s
        if s.exiting:
            return "shutting_down"
        if s.stopped:
            return "stopped"
        if s.interrupt_requested or s.stop_requested:
            return "stopping"
        if s.error_paused:
            return "verification_abort" if s.pause_reason == "verification_abort" else "paused_on_error"
        if self._failure is not None:
            return self._failure[0]
        if s.waiting_for_input:
            # `vbc serve` waits for the next refresh after every pass: idle.
            # This is the only reliable end-of-pass signal: ProcessingFinished
            # (s.finished) comes before the auto-repair / error-move step.
            return "idle"
        if s.repair.active:
            return "repairing"
        if not s.discovery.finished:
            return "discovering"
        return "running"

    def _throughput_and_eta_unlocked(self, now: float) -> Tuple[Optional[dto.Throughput], Optional[int]]:
        """Same formulas as the legacy dashboard (30 s window, avg s/file)."""
        s = self._s
        c = s.counters
        if s.processing_started_at is None or (c.completed == 0 and c.failed == 0):
            return None, None
        elapsed = now - s.processing_started_at
        cutoff = now - THROUGHPUT_WINDOW_S
        bytes_window = files_window = 0
        for ts, size in reversed(s.throughput_history):
            if ts < cutoff:
                break
            bytes_window += size
            files_window += 1
        time_window = min(elapsed, THROUGHPUT_WINDOW_S)
        if time_window > 0.1 and bytes_window > 0:
            rate = bytes_window / time_window
        elif elapsed > 0:
            rate = s.total_input_bytes / elapsed
        else:
            rate = 0.0
        throughput = dto.Throughput(bytes_per_s=rate, at=to_ms(now))

        done_since = (c.completed - c.completed_at_last_discovery) + (c.failed - c.failed_at_last_discovery)
        remaining = s.discovery.files_to_process - done_since
        eta_at = None
        if remaining > 0:
            per_file = 0.0
            if files_window > 0 and time_window > 0:
                per_file = time_window / files_window
            elif (c.completed + c.failed) > 0 and elapsed > 0:
                per_file = elapsed / (c.completed + c.failed)
            if per_file > 0:
                eta_at = to_ms(now + per_file * remaining)
        return throughput, eta_at

    def snapshot(self) -> dto.StateSnapshot:
        with self._lock:
            now = self._clock()
            s = self._s
            throughput, eta_at = self._throughput_and_eta_unlocked(now)
            active = []
            for job_id, a in s.active.items():
                job = a.job
                active.append(dto.ActiveJob(
                    id=job_id,
                    name=job.source_file.path.name,
                    path=str(job.source_file.path),
                    status=job.status.value if hasattr(job.status, "value") else str(job.status),
                    started_at=to_ms(a.started_at),
                    input_bytes=job.source_file.size_bytes,
                    part_count=job.source_file.part_count,
                    **_display_fields(job),
                    progress=None if a.anchor is None else dto.ProgressAnchor(
                        percent=a.anchor.percent, at=to_ms(a.anchor.at), rate=a.anchor.rate,
                    ),
                ))
            return dto.StateSnapshot(
                rev=self._rev,
                server_time=to_ms(now),
                run_status=self._run_status_unlocked(),
                status_message=s.status_message,
                counters=s.counters.model_copy(),
                discovery=s.discovery.model_copy(),
                total_input_bytes=s.total_input_bytes,
                total_output_bytes=s.total_output_bytes,
                throughput=throughput,
                eta_at=eta_at,
                processing_started_at=to_ms(s.processing_started_at),
                threads=self._threads,
                stop_requested=s.stop_requested,
                interrupt_requested=s.interrupt_requested,
                repair=s.repair.model_copy(),
                active_jobs=active,
                recent_jobs=list(s.recent)[:RECENT_JOBS_IN_SNAPSHOT],
                queue=dto.QueueSummary(count=len(s.pending), bytes=sum(v.size_bytes for v in s.pending)),
                last_action=None if s.last_action is None else dto.LastAction(
                    text=s.last_action[0], at=to_ms(s.last_action[1]),
                ),
                errors_count=len(self._errors),
                errors_last_id=self._error_seq,
                config_rev=self._config_rev,
                dirs_rev=self._dirs_rev,
                gpu_last_seq=self._gpu_seq,
                encoder=self._encoder,
                encoder_note=self._encoder_note,
                server_version=vbc_version(),
                source_deletion=self._source_deletion(),
            )

    def queue(self, offset: int = 0, limit: int = 100) -> dto.QueuePage:
        with self._lock:
            items = self._s.pending[offset: offset + limit]
            return dto.QueuePage(
                rev=self._rev, server_time=to_ms(self._clock()), offset=offset, total=len(self._s.pending),
                items=[
                    dto.QueueItem(path=str(v.path), name=v.path.name, size_bytes=v.size_bytes,
                                  part_count=v.part_count)
                    for v in items
                ],
            )

    def pending_files(self) -> list:
        with self._lock:
            return list(self._s.pending)

    def recent_jobs(self, limit: int = 100) -> dto.RecentJobsPage:
        with self._lock:
            return dto.RecentJobsPage(
                rev=self._rev, server_time=to_ms(self._clock()), items=list(self._s.recent)[:limit],
            )
