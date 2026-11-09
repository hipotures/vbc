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
    warning: Optional[str] = None   # completed, but e.g. the source could not be deleted
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
    sources_deleted: int = 0   # source files deleted after success since the server started
    warnings: int = 0          # completed jobs with a warning (e.g. source not deleted)


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
    server_version: Optional[str] = None
    source_deletion: bool = True  # False: no policy in force deletes sources ("deleted: off")  # VBC version of the server (image release)


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
    # From nvidia-smi (None when the server reads nvtop):
    enc_util_pct: Optional[float] = None
    dec_util_pct: Optional[float] = None
    mem_used_mib: Optional[float] = None
    mem_total_mib: Optional[float] = None
    power_limit_w: Optional[float] = None
    gpu_clock_mhz: Optional[float] = None
    mem_clock_mhz: Optional[float] = None
    throttle: Optional[str] = None
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


class ConfigPushRequest(_Dto):
    """A whole vbc.yaml from a client (`vbc config --push FILE`)."""

    yaml: str
    restart: bool = True  # restart the server now (running jobs finish first)


class ConfigPushResult(Envelope):
    """The pushed config was saved; it takes effect at the next server start."""

    path: str
    backup: Optional[str] = None
    changed: List[str] = Field(default_factory=list)  # dotted keys that differ
    kept_from_server: List[str] = Field(default_factory=list)
    restart_required: bool = True   # saved but not loaded yet (no restart requested)
    restarting: bool = False        # the server restarts with it after running jobs finish


class SourceCleanupRequest(_Dto):
    """Find sources that already have a verified output; delete them only with ``delete``."""

    delete: bool = False
    dirs: List[str] = Field(default_factory=list)  # default: enabled non-manifest folders


class SourceCleanupProblem(_Dto):
    path: str
    reason: str


class SourceCleanupStatus(_Dto):
    state: Literal["idle", "running", "done", "failed"]
    delete: bool = False
    folders: List[str] = Field(default_factory=list)
    total: int = 0          # sources that have an output file
    checked: int = 0
    duplicates: int = 0     # verified: output tags name this source (name + size), ffprobe OK
    duplicate_bytes: int = 0
    deleted: int = 0
    deleted_bytes: int = 0
    problems_count: int = 0
    problems: List[SourceCleanupProblem] = Field(default_factory=list)  # first 500
    started_at: Optional[int] = None
    finished_at: Optional[int] = None
    error: Optional[str] = None


class ManifestPathRequest(_Dto):
    path: str           # server path: a failed .json/.err, or a folder (repair)
    dry_run: bool = False


class RestoredSource(_Dto):
    archive: str
    original: str


class ManifestRestoreResult(_Dto):
    manifest: str
    destination: str
    dry_run: bool
    restored_sources: List[RestoredSource] = Field(default_factory=list)
    already_present: List[str] = Field(default_factory=list)
    error_marker: Optional[str] = None


class ManifestRepairOutcome(_Dto):
    manifest: str
    status: str   # READY (dry run), REPAIRED, SKIPPED, FAILED
    detail: str


class ManifestRepairStatus(_Dto):
    state: Literal["idle", "running", "done", "failed"]
    target: Optional[str] = None
    dry_run: bool = False
    total: int = 0
    done: int = 0
    current: Optional[str] = None
    outcomes: List[ManifestRepairOutcome] = Field(default_factory=list)
    started_at: Optional[int] = None
    finished_at: Optional[int] = None
    error: Optional[str] = None


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
