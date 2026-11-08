"""Transport-only DTOs of the VBC HTTP API (``/api/v1``).

This is the only module clients may share with the server. It depends on
nothing but pydantic, never on server internals. All times are epoch
milliseconds (``*_at``); clients correct clock skew with ``server_time``.
See docs/architecture/service-split.md ("API (v1)", "Changing counters
without streaming every tick").
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

RunStatus = Literal[
    "discovering",
    "running",
    "repairing",
    "paused_on_error",
    "waiting_for_input",
    "verification_abort",
    "stopping",       # POST /stop accepted: running jobs finish (or are interrupted)
    "stopped",        # processing stopped; the server keeps running, refresh resumes
    "shutting_down",  # the server process got a signal and is exiting
    "idle",
    "failed",
]


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Envelope(_Dto):
    """Fields carried by every response."""

    schema_version: int = SCHEMA_VERSION
    rev: int
    server_time: int


# ── /state ──────────────────────────────────────────────────────────────────


class ProgressAnchor(_Dto):
    """Client predicts ``min(percent + rate * (now - at) / 1000, 99.5)``."""

    percent: float
    at: int
    rate: float = Field(description="percent per second")


class MediaInfo(_Dto):
    """Source stream facts the server already knows (for display only)."""

    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    duration_s: Optional[float] = None
    codec: Optional[str] = None
    audio_codec: Optional[str] = None
    camera_model: Optional[str] = None
    custom_cq: Optional[int] = None


class ActiveJob(_Dto):
    id: str
    name: str
    path: str
    status: str
    started_at: int
    input_bytes: int
    quality: Optional[str] = None
    quality_value: Optional[int] = None
    rotation_angle: Optional[int] = None
    config_source: Optional[str] = None
    part_count: int = 1
    media: Optional[MediaInfo] = None
    progress: Optional[ProgressAnchor] = None


class RecentJob(_Dto):
    id: str
    name: str
    path: str
    status: str
    finished_at: int
    input_bytes: int
    output_bytes: Optional[int] = None
    duration_s: Optional[float] = None
    error_message: Optional[str] = None
    quality: Optional[str] = None
    quality_value: Optional[int] = None
    rotation_angle: Optional[int] = None
    config_source: Optional[str] = None
    media: Optional[MediaInfo] = None


class Counters(_Dto):
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    hw_cap: int = 0
    cam_skipped: int = 0
    min_ratio_skip: int = 0
    interrupted: int = 0
    ignored_av1: int = 0
    completed_at_last_discovery: int = 0
    failed_at_last_discovery: int = 0
    session_completed_base: int = 0


class Discovery(_Dto):
    finished: bool = False
    finished_at: Optional[int] = None
    files_found: int = 0
    files_to_process: int = 0
    already_compressed: int = 0
    ignored_small: int = 0
    ignored_err: int = 0
    source_folders_count: int = 1


class Throughput(_Dto):
    bytes_per_s: float
    at: int


class Threads(_Dto):
    current: int
    minimum: int
    maximum: int


class Repair(_Dto):
    active: bool = False
    candidates: int = 0
    repaired: int = 0


class LastAction(_Dto):
    text: str
    at: int


class QueueSummary(_Dto):
    count: int = 0
    bytes: int = 0


class StateSnapshot(Envelope):
    run_status: RunStatus
    status_message: Optional[str] = None
    counters: Counters
    discovery: Discovery
    total_input_bytes: int = 0
    total_output_bytes: int = 0
    throughput: Optional[Throughput] = None
    eta_at: Optional[int] = None
    processing_started_at: Optional[int] = None
    threads: Threads
    stop_requested: bool = False
    interrupt_requested: bool = False
    repair: Repair
    active_jobs: List[ActiveJob]
    recent_jobs: List[RecentJob]
    queue: QueueSummary
    last_action: Optional[LastAction] = None
    errors_count: int = 0
    errors_last_id: int = 0
    config_rev: int = 0
    dirs_rev: int = 0
    gpu_last_seq: int = 0
    # Encoder chosen at server start: "gpu" (NVENC works) or "cpu"; with CPU,
    # encoder_note says why the GPU is not used.
    encoder: Optional[Literal["gpu", "cpu"]] = None
    encoder_note: Optional[str] = None


# ── paginated reads ─────────────────────────────────────────────────────────


class QueueItem(_Dto):
    path: str
    name: str
    size_bytes: int
    part_count: int = 1


class QueuePage(Envelope):
    offset: int
    total: int
    items: List[QueueItem]


class RecentJobsPage(Envelope):
    items: List[RecentJob]


class ErrorEntry(_Dto):
    id: int
    created_at: int
    path: str
    job_id: Optional[str] = None
    stage: Optional[str] = None
    error_message: str
    exit_code: Optional[int] = None
    manifest_request_id: Optional[str] = None
    size_bytes: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    codec: Optional[str] = None
    audio_codec: Optional[str] = None
    duration_s: Optional[float] = None


class ErrorsPage(Envelope):
    last_id: int
    entries: List[ErrorEntry]


class GpuSample(_Dto):
    seq: int
    at: int
    available: bool
    temp_c: Optional[float] = None
    power_w: Optional[float] = None
    gpu_util_pct: Optional[float] = None
    mem_util_pct: Optional[float] = None
    fan_pct: Optional[float] = None
    raw: Optional[dict] = None


class GpuPage(Envelope):
    enabled: bool
    last_seq: int
    samples: List[GpuSample]


class LogEntry(_Dto):
    seq: int
    ts: int
    level: str
    logger: str
    message: str


class LogsPage(Envelope):
    last_seq: int
    entries: List[LogEntry]


# ── configuration window ────────────────────────────────────────────────────


class ConfigView(Envelope):
    """Effective configuration, structured per section (no preformatted text)."""

    config_rev: int
    sections: Dict[str, Dict[str, Any]]


class IoDir(_Dto):
    path: str
    enabled: bool
    metadata: bool = False
    exists: bool
    readable: bool
    output_dir: Optional[str] = None
    errors_dir: Optional[str] = None
    output_writable: Optional[bool] = None
    pending_count: int = 0
    pending_bytes: int = 0


class IoView(Envelope):
    dirs: List[IoDir]
    suffix_output_dirs: Optional[str] = None
    suffix_errors_dirs: Optional[str] = None
    log_path: Optional[str] = None
    queue_sort: str
    queue_seed: Optional[int] = None
    storage_roots: List[str] = Field(default_factory=list)


# ── dirs ────────────────────────────────────────────────────────────────────


class DirEntry(_Dto):
    """One input folder. ``metadata``: the folder holds JSON manifests (multipart
    jobs) instead of videos. ``watch``: new files start work without a refresh.
    Fields left out (null) keep the saved values of a listed folder; for a new
    folder the server detects them: manifests inside -> metadata and watch,
    network file system -> polling (inotify misses other hosts' writes)."""

    path: str
    enabled: bool
    metadata: Optional[bool] = None
    watch: Optional[bool] = None
    watch_mode: Optional[Literal["inotify", "polling"]] = None
    poll_interval_seconds: Optional[float] = Field(default=None, gt=0)


class DirStatus(DirEntry):
    metadata: bool
    watch: bool
    exists: bool
    readable: bool


class DirsState(Envelope):
    dirs_rev: int
    entries: List[DirStatus]


class DirsScanResult(DirsState):
    """``POST /dirs/scan``: media mounts newly added to the list (disabled)."""

    added: List[str] = Field(default_factory=list)


# ── commands ────────────────────────────────────────────────────────────────


class SetThreadsRequest(_Dto):
    target: int


class StopRequest(_Dto):
    """Stop processing (the server keeps running; refresh resumes)."""

    mode: Literal["graceful", "interrupt"] = "graceful"


class PutDirsRequest(_Dto):
    entries: List[DirEntry]


class LogLevelRequest(_Dto):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Ack(Envelope):
    """Response of a command that was accepted (effects show up in /state)."""

    accepted: bool = True


class ThreadsResult(Envelope):
    threads: Threads


class Problem(_Dto):
    """Error body for 4xx responses (field errors for 422)."""

    status: int
    title: str
    detail: Optional[str] = None
    errors: List[str] = Field(default_factory=list)
