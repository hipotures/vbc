"""Pipeline orchestrator for video compression job lifecycle management.

Coordinates file discovery, queue management, metadata extraction, compression jobs,
and dynamic thread control. Uses event-driven architecture with EventBus for loose
coupling between pipeline and UI layers.

Key responsibilities:
- Discover video files matching extensions and size filters
- Extract and cache video metadata (codec, FPS, camera model, etc.)
- Manage queue of pending jobs with configurable sort order
- Submit jobs to thread pool respecting prefetch_factor (submit-on-demand pattern)
- Handle job lifecycle: discovery → queuing → processing → completion/failure
- Support dynamic thread count adjustment and graceful shutdown
- Emit events for UI updates (JobStarted, JobCompleted, JobFailed, etc.)
"""

import re
import hashlib
import json
import threading
import concurrent.futures
import shutil
import logging
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Union, TYPE_CHECKING, Tuple
from vbc.config.models import AppConfig, InputDirEntry, MetadataConfig
from vbc.config.rate_control import (
    ResolvedRateControl,
    resolve_rate_control_values,
    format_bps_human,
    parse_rate_cap_bps,
    SVT_AV1_TARGET_MAX_BPS,
    SVT_AV1_TARGET_MAX_KBPS,
)

if TYPE_CHECKING:
    from vbc.config.local_registry import LocalConfigRegistry
    from vbc.config.overrides import CliConfigOverrides
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.file_scanner import FileScanner
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.infrastructure.ffprobe import FFprobeAdapter
from vbc.infrastructure.ffmpeg import (
    FFmpegAdapter,
    select_encoder_args,
    extract_quality_value,
    extract_quality_flag,
    output_extension_for_args,
    infer_encoder_label,
)
from vbc.infrastructure.exiftool_tmp import remove_exiftool_tmp_for_target
from vbc.domain.models import (
    CompressionJob,
    CompressionManifest,
    JobStatus,
    MetadataRequest,
    MultipartPart,
    VideoFile,
    VideoMetadata,
)
from vbc.domain.events import (
    DiscoveryErrorEntry,
    DiscoveryStarted,
    DiscoveryFinished,
    JobStarted,
    JobCompleted,
    JobFailed,
    QueueUpdated,
    ProcessingFinished,
    RefreshFinished,
    RequestShutdown,
    ThreadControlEvent,
    InterruptRequested,
)
from vbc.pipeline.error_file_mover import collect_error_entries, move_failed_files
from vbc.pipeline.queue_sorting import sort_files
from vbc.pipeline.repair import process_repairs

_REQUIRED_VBC_VERIFY_TAGS = {
    "vbcoriginalname",
    "vbcoriginalsize",
    "vbcquality",
    "vbcoriginalbitrate",
    "vbcencoder",
    "vbcfinishedat",
}


class VerificationAbortError(RuntimeError):
    """Raised when verification_fail_action='exit' requests process termination."""


def _emit_bell() -> None:
    """Write two terminal bells 0.3s apart directly to /dev/tty, bypassing Rich's stdout capture."""
    import time
    def _ring():
        try:
            with open('/dev/tty', 'w') as tty:
                tty.write('\x07')
                tty.flush()
        except OSError:
            import sys
            sys.stderr.write('\x07')
            sys.stderr.flush()
    _ring()
    time.sleep(0.3)
    _ring()


class Orchestrator:
    """Video compression pipeline orchestrator.

    Manages the full job lifecycle: discovery → queuing → compression.
    Coordinates with infrastructure adapters (FFmpeg, ExifTool, FFprobe)
    and publishes events to the UI via EventBus.

    Uses thread-safe state management with Condition variables for:
    - Dynamic thread pool sizing (Ctrl+< and Ctrl+>)
    - Graceful shutdown coordination (Ctrl+S)
    - Queue refresh signaling (Ctrl+R)

    Implements "submit-on-demand" pattern: submits only prefetch_factor*threads
    jobs to thread pool; submits new jobs as workers complete (avoids queueing
    thousands of futures for large directories).

    Args:
        config: AppConfig with general, GPU, UI, autorotate, and input/output settings.
        event_bus: EventBus for publishing job lifecycle events.
        file_scanner: FileScanner for discovering video files.
        exif_adapter: ExifToolAdapter for metadata extraction (camera, GPS, etc.).
        ffprobe_adapter: FFprobeAdapter for codec, FPS, duration probing.
        ffmpeg_adapter: FFmpegAdapter for AV1 compression execution.
        output_dir_map: Optional override mapping input_dir → output_dir (else uses suffix).
    """

    def __init__(
        self,
        config: AppConfig,
        event_bus: EventBus,
        file_scanner: FileScanner,
        exif_adapter: ExifToolAdapter,
        ffprobe_adapter: FFprobeAdapter,
        ffmpeg_adapter: FFmpegAdapter,
        output_dir_map: Optional[Dict[Path, Path]] = None,
        errors_dir_map: Optional[Dict[Path, Path]] = None,
        local_config_registry: Optional["LocalConfigRegistry"] = None,
        cli_overrides: Optional["CliConfigOverrides"] = None,
        config_path: Optional[Path] = None,
        input_dir_entries: Optional[Dict[Path, InputDirEntry]] = None,
    ):
        self.config = config
        self.event_bus = event_bus
        self.file_scanner = file_scanner
        self.exif_adapter = exif_adapter
        self.ffprobe_adapter = ffprobe_adapter
        self.ffmpeg_adapter = ffmpeg_adapter
        self.logger = logging.getLogger(__name__)

        # Local config registry and CLI overrides for per-job config resolution
        self.local_registry = local_config_registry
        self.cli_overrides = cli_overrides
        self.config_path = config_path
        self._metadata_config = config.metadata.model_copy(deep=True)
        self._metadata_config_lock = threading.Lock()
        self._manifest_inflight: set[Path] = set()
        self._manifest_inflight_lock = threading.Lock()
        self._input_dir_entries = input_dir_entries
        if self._input_dir_entries is None:
            self._input_dir_entries = {
                Path(entry.path): entry for entry in config.input_dirs if entry.enabled
            }
        self._metadata_input_entries = {
            path: entry
            for path, entry in self._input_dir_entries.items()
            if entry.metadata
        }

        # Metadata cache (thread-safe)
        self._metadata_cache = {}  # Path -> VideoMetadata
        self._metadata_lock = threading.Lock()
        self._metadata_failure_counts: Dict[Path, int] = {}
        self._metadata_failure_limit = 1
        self._metadata_failure_reasons: Dict[Path, str] = {}
        self._metadata_failed_paths: set[Path] = set()
        self._metadata_failed_reported: set[Path] = set()

        # Dynamic control state
        self._shutdown_requested = False
        self._current_max_threads = config.general.threads
        self._active_threads = 0
        self._thread_lock = threading.Condition()
        self._refresh_requested = False
        self._refresh_lock = threading.Lock()
        self._shutdown_event = threading.Event()  # Signal workers to stop
        self._wait_event = threading.Event()       # Signals wait loop to unblock
        self._restart_after_wait = False           # True = R pressed; False = S/Ctrl+C
        self._pause_requested = False
        self._pause_message: Optional[str] = None
        self._verification_abort_message: Optional[str] = None

        # Stats
        self.skipped_vbc_count = 0
        self._stats_lock = threading.Lock()

        # Folder mapping (input_dir -> output_dir)
        self._folder_mapping: Dict[Path, Path] = {}
        self._output_dir_map_override: Dict[Path, Path] = output_dir_map or {}
        self._use_output_dir_map_override = output_dir_map is not None
        self._errors_dir_map: Dict[Path, Path] = errors_dir_map or {}
        self._session_error_sources: Dict[Path, Path] = {}
        self._auto_repair_attempted_sources: set[Path] = set()
        self._repair_lock = threading.Lock()

        # Dynamic input dirs (updated via InputDirsChanged event)
        self._pending_input_dirs: Optional[List[Path]] = None

        self._setup_subscriptions()

    def _setup_subscriptions(self):
        from vbc.domain.events import RefreshRequested, InputDirsChanged
        self.event_bus.subscribe(RequestShutdown, self._on_shutdown_request)
        self.event_bus.subscribe(ThreadControlEvent, self._on_thread_control)
        self.event_bus.subscribe(RefreshRequested, self._on_refresh_request)
        self.event_bus.subscribe(InterruptRequested, self._on_interrupt_requested)
        self.event_bus.subscribe(InputDirsChanged, self._on_input_dirs_changed)

    def _normalize_input_dirs(self, input_dirs: Union[Path, List[Path]]) -> List[Path]:
        if isinstance(input_dirs, (list, tuple)):
            return [Path(p) for p in input_dirs]
        return [Path(input_dirs)]

    def _on_shutdown_request(self, event: RequestShutdown):
        with self._thread_lock:
            # Toggle shutdown state (press S again to cancel)
            if self._shutdown_requested:
                self._shutdown_requested = False
                message = "SHUTDOWN cancelled"
            else:
                self._shutdown_requested = True
                message = "SHUTDOWN requested (press S to cancel)"
                self._wait_event.set()  # Wake up wait loop if waiting
            self._thread_lock.notify_all()
        # Publish feedback message
        from vbc.domain.events import ActionMessage
        self.event_bus.publish(ActionMessage(message=message))

    def _on_thread_control(self, event: ThreadControlEvent):
        if self._shutdown_requested:
            return

        old_val = self._current_max_threads
        with self._thread_lock:
            requested = self._current_max_threads + event.change
            self._current_max_threads = max(1, min(8, requested))
            self._thread_lock.notify_all()
        # Publish feedback message (like old vbc.py lines 769, 776)
        from vbc.domain.events import ActionMessage
        if self._current_max_threads != old_val:
            self.event_bus.publish(ActionMessage(message=f"Threads: {old_val} → {self._current_max_threads}"))
        elif requested > self._current_max_threads:
            self.event_bus.publish(ActionMessage(message=f"Threads: {self._current_max_threads} (max)"))
        elif requested < self._current_max_threads:
            self.event_bus.publish(ActionMessage(message=f"Threads: {self._current_max_threads} (min)"))

    def _on_input_dirs_changed(self, event) -> None:
        """Update active input dirs for the next re-scan cycle."""
        self._pending_input_dirs = [Path(d) for d in event.active_dirs]

    def _on_refresh_request(self, event):
        with self._refresh_lock:
            self._refresh_requested = True
        # Also wake the wait loop (if active) with restart intent
        self._restart_after_wait = True
        self._wait_event.set()

    def _on_interrupt_requested(self, event: InterruptRequested):
        """Handle Ctrl+C interrupt from keyboard listener."""
        self.logger.info("Interrupt requested (Ctrl+C) - stopping orchestrator...")
        from vbc.domain.events import ActionMessage
        self.event_bus.publish(ActionMessage(message="Ctrl+C - interrupting active compressions..."))

        # Signal all workers to stop immediately
        self._shutdown_event.set()

        # Stop accepting new tasks
        with self._thread_lock:
            self._shutdown_requested = True
            self._thread_lock.notify_all()

        # Wake up wait loop if waiting
        self._wait_event.set()

    def _get_output_dir(self, input_dir: Path) -> Path:
        """Get output directory for given input directory."""
        mapped = self._folder_mapping.get(input_dir)
        if mapped is not None:
            return mapped
        if self._use_output_dir_map_override:
            mapped = self._output_dir_map_override.get(input_dir)
            if mapped is None:
                # New dir added at runtime — fall back to suffix
                suffix = self.config.suffix_output_dirs
                if not suffix:
                    raise ValueError(f"Output directory mapping missing for {input_dir}")
                return input_dir.with_name(f"{input_dir.name}{suffix}")
            return mapped
        if self.config.output_dirs:
            raise ValueError(f"Output directory mapping missing for {input_dir}")
        suffix = self.config.suffix_output_dirs
        if not suffix:
            raise ValueError("suffix_output_dirs is not set.")
        return input_dir.with_name(f"{input_dir.name}{suffix}")

    def _resolve_output_dir(self, input_dir: Path, input_index: int) -> Path:
        if self.config.output_dirs:
            if input_index >= len(self.config.output_dirs):
                raise ValueError("output_dirs count must match input_dirs count.")
            return Path(self.config.output_dirs[input_index])
        suffix = self.config.suffix_output_dirs
        if not suffix:
            raise ValueError("suffix_output_dirs is not set.")
        return input_dir.with_name(f"{input_dir.name}{suffix}")

    def _find_input_folder(self, file_path: Path) -> Optional[Path]:
        """Find which input folder contains this file."""
        for input_dir in self._folder_mapping.keys():
            try:
                file_path.relative_to(input_dir)
                return input_dir
            except ValueError:
                continue
        return None

    def _get_metadata(self, video_file: VideoFile, base_metadata: Optional[Dict[str, Any]] = None) -> Optional[VideoMetadata]:
        """Get metadata with thread-safe caching (ffprobe + ExifTool like legacy)."""
        file_path = video_file.path
        # Check if already cached
        with self._metadata_lock:
            cached = self._metadata_cache.get(file_path)
            if cached is not None:
                return cached
            failures = self._metadata_failure_counts.get(file_path, 0)
            if base_metadata is None and failures >= self._metadata_failure_limit:
                return None
            attempt = failures + 1

        # Not in cache, extract it
        try:
            if self.config.general.debug:
                self.logger.debug(
                    f"Metadata cache miss: {file_path.name} "
                    f"(attempt {attempt}/{self._metadata_failure_limit})"
                )

            stream_info = base_metadata or self.ffprobe_adapter.get_stream_info(file_path)
            metadata = self._build_metadata(video_file, stream_info)

            # Cache it
            with self._metadata_lock:
                self._metadata_cache[file_path] = metadata
                self._metadata_failure_counts.pop(file_path, None)

            return metadata
        except Exception as e:
            with self._metadata_lock:
                failures = self._metadata_failure_counts.get(file_path, 0) + 1
                self._metadata_failure_counts[file_path] = failures
                failure_limit = self._metadata_failure_limit
            if failures >= failure_limit:
                if base_metadata is None:
                    self._register_metadata_failure(video_file, e)
                self.logger.warning(
                    f"Failed to extract metadata for {file_path.name} "
                    f"(attempt {failures}/{failure_limit}); suppressing retries: {e}"
                )
            else:
                self.logger.warning(
                    f"Failed to extract metadata for {file_path.name} "
                    f"(attempt {failures}/{failure_limit}): {e}"
                )
            return None

    def _register_metadata_failure(self, video_file: VideoFile, error: Exception) -> None:
        file_path = video_file.path
        if file_path in self._metadata_failed_paths:
            return
        err_msg = "File is corrupted (ffprobe failed to read). Skipped."
        self._metadata_failed_paths.add(file_path)
        self._metadata_failure_reasons[file_path] = err_msg

        output_path = self._write_error_marker(video_file, err_msg)
        if output_path:
            self.logger.error(f"Corrupted file detected (ffprobe failed): {video_file.path.name} - {error}")
        else:
            self.logger.warning(
                f"Failed to write error marker for {video_file.path.name} after ffprobe error: {error}"
            )
        self._publish_metadata_failed_job(video_file, err_msg, output_path)

    def _publish_metadata_failed_job(
        self,
        video_file: VideoFile,
        err_msg: str,
        output_path: Optional[Path],
    ) -> None:
        file_path = video_file.path
        if file_path in self._metadata_failed_reported:
            return
        self._metadata_failed_reported.add(file_path)
        job = CompressionJob(
            source_file=video_file,
            status=JobStatus.FAILED,
            output_path=output_path,
            error_message=err_msg,
        )
        self.event_bus.publish(JobFailed(job=job, error_message=err_msg))

    def _write_error_marker(self, video_file: VideoFile, err_msg: str) -> Optional[Path]:
        input_dir = self._find_input_folder(video_file.path)
        if not input_dir:
            return None
        output_dir = self._folder_mapping.get(input_dir)
        if output_dir is None:
            try:
                output_dir = self._get_output_dir(input_dir)
            except Exception:
                return None
            self._folder_mapping[input_dir] = output_dir
        try:
            rel_path = video_file.path.relative_to(input_dir)
        except ValueError:
            rel_path = Path(video_file.path.name)
        output_suffix = self._output_suffix_for_mode()
        output_path = output_dir / rel_path.with_suffix(output_suffix)
        err_path = output_path.with_suffix(".err")
        try:
            self._write_job_error_marker(video_file, err_path, err_msg)
        except Exception:
            return None
        return output_path

    def _write_job_error_marker(self, video_file: VideoFile, err_path: Path, err_msg: str) -> None:
        err_path.parent.mkdir(parents=True, exist_ok=True)
        err_path.write_text(err_msg)
        self._record_session_error_marker(video_file.path, err_path)

    def _record_session_error_marker(self, source_path: Path, err_path: Path) -> None:
        with self._repair_lock:
            self._session_error_sources[err_path] = source_path

    def _get_errors_dir(self, input_dir: Path) -> Optional[Path]:
        mapped = self._errors_dir_map.get(input_dir)
        if mapped is not None:
            return mapped
        if self.config.errors_dirs:
            return None
        suffix = self.config.suffix_errors_dirs
        if not suffix:
            return None
        mapped = input_dir.with_name(f"{input_dir.name}{suffix}")
        self._errors_dir_map[input_dir] = mapped
        return mapped

    def _collect_session_repair_entries(self, input_dirs: List[Path]) -> List[Tuple[Path, Path, Path, Path]]:
        output_dir_map = {
            input_dir: self._get_output_dir(input_dir)
            for input_dir in input_dirs
        }
        errors_dir_map = {
            input_dir: errors_dir
            for input_dir in input_dirs
            if (errors_dir := self._get_errors_dir(input_dir)) is not None
        }
        if not errors_dir_map:
            return []

        all_entries = collect_error_entries(input_dirs, output_dir_map, errors_dir_map)
        with self._repair_lock:
            session_error_sources = dict(self._session_error_sources)
            attempted_sources = set(self._auto_repair_attempted_sources)

        repair_entries: List[Tuple[Path, Path, Path, Path]] = []
        for entry in all_entries:
            err_file = entry[3]
            source_path = session_error_sources.get(err_file)
            if source_path is None:
                continue
            if source_path in attempted_sources:
                continue
            repair_entries.append(entry)
        return repair_entries

    @staticmethod
    def _video_file_from_path(path: Path) -> Optional[VideoFile]:
        try:
            stat = path.stat()
        except OSError:
            return None
        return VideoFile(path=path, size_bytes=stat.st_size)

    def _run_auto_repair(self, input_dirs: List[Path]) -> List[Path]:
        if not self.config.general.auto_repair_errors:
            return []

        repair_entries = self._collect_session_repair_entries(input_dirs)
        if not repair_entries:
            return []

        from vbc.domain.events import ActionMessage, RepairFinished, RepairStarted

        self.logger.info(
            f"Auto-repair pass: {len(repair_entries)} candidate(s) from current session"
        )
        self.event_bus.publish(RepairStarted(candidate_count=len(repair_entries)))
        self.event_bus.publish(ActionMessage(message=f"REPAIR started: {len(repair_entries)} files"))

        moved_files = move_failed_files(
            input_dirs,
            {input_dir: self._get_output_dir(input_dir) for input_dir in input_dirs},
            self._errors_dir_map,
            self.config.general.extensions,
            logger=self.logger,
            error_entries=repair_entries,
        )

        with self._repair_lock:
            for entry in repair_entries:
                source_path = self._session_error_sources.get(entry[3])
                if source_path is not None:
                    self._auto_repair_attempted_sources.add(source_path)

        repaired_count = 0
        repaired_paths: List[Path] = []
        if moved_files:
            repaired_count, repaired_paths = process_repairs(
                input_dirs,
                self._errors_dir_map,
                self.config.general.extensions,
                logger=self.logger,
                target_files=moved_files,
                return_repaired_files=True,
                auto_repair=True,
            )

        with self._repair_lock:
            for repaired_path in repaired_paths:
                self._auto_repair_attempted_sources.add(repaired_path)

        self.event_bus.publish(RepairFinished(attempted=len(repair_entries), repaired=repaired_count))
        self.event_bus.publish(ActionMessage(message=f"REPAIR finished: {repaired_count}/{len(repair_entries)} repaired"))
        self.logger.info(
            f"Auto-repair pass complete: {repaired_count}/{len(repair_entries)} repaired, "
            f"{len(repaired_paths)} file(s) queued for compression"
        )
        return repaired_paths

    def _prune_failed_pending(self, pending) -> int:
        if not self._metadata_failed_paths:
            return 0
        failed_paths = self._metadata_failed_paths
        if not failed_paths:
            return 0
        from collections import deque
        kept = deque()
        removed = 0
        while pending:
            vf = pending.popleft()
            if vf.path in failed_paths:
                removed += 1
                continue
            kept.append(vf)
        pending.extend(kept)
        return removed

    def _build_metadata(self, video_file: VideoFile, stream_info: Dict[str, Any]) -> VideoMetadata:
        width = int(stream_info.get("width", 0) or 0)
        height = int(stream_info.get("height", 0) or 0)
        megapixels = round(width * height / 1_000_000) if width and height else None
        
        # Check vbc_encoded from ffprobe
        vbc_encoded = bool(stream_info.get("vbc_encoded", False))
        
        metadata = VideoMetadata(
            width=width,
            height=height,
            codec=str(stream_info.get("codec", "unknown") or "unknown"),
            audio_codec=stream_info.get("audio_codec"),
            fps=float(stream_info.get("fps") or 0.0),
            bitrate_kbps=stream_info.get("bitrate_kbps"),
            megapixels=megapixels,
            color_space=stream_info.get("color_space"),
            pix_fmt=stream_info.get("pix_fmt"),
            duration=float(stream_info.get("duration") or 0.0),
            vbc_encoded=vbc_encoded,
        )

        if self.config.general.use_exif:
            try:
                exif_info = self.exif_adapter.extract_exif_info(video_file, self.config.general.dynamic_quality)
                metadata.camera_model = exif_info.get("camera_model")
                metadata.camera_raw = exif_info.get("camera_raw")
                metadata.custom_cq = exif_info.get("custom_cq")
                exif_bitrate_kbps = exif_info.get("bitrate_kbps")
                if exif_bitrate_kbps is not None:
                    metadata.bitrate_kbps = exif_bitrate_kbps
                # Merge vbc_encoded from ExifTool (more reliable for XMP)
                if exif_info.get("vbc_encoded"):
                    metadata.vbc_encoded = True
                    
                matched_pattern = exif_info.get("matched_pattern")
                if self.config.general.debug and matched_pattern:
                    raw_model = metadata.camera_raw or "None"
                    matched_rule = self.config.general.dynamic_quality.get(str(matched_pattern))
                    has_rate_rule = bool(matched_rule and matched_rule.rate is not None)
                    cq_value = metadata.custom_cq if metadata.custom_cq is not None else "none"
                    self.logger.debug(
                        f"DYNAMIC_QUALITY_MATCH: {video_file.path.name} "
                        f"pattern=\"{matched_pattern}\" raw=\"{raw_model}\" "
                        f"quality_mode={self.config.general.quality_mode} cq={cq_value} "
                        f"has_rate_rule={has_rate_rule}"
                    )
            except Exception as e:
                if self.config.general.debug:
                    self.logger.debug(f"ExifTool analysis failed for {video_file.path.name}: {e}")

        return metadata

    def _determine_cq(self, file: VideoFile, use_gpu: Optional[bool] = None, config: Optional[AppConfig] = None) -> int:
        """Determine the quality value based on camera model and encoder defaults.

        Args:
            file: Video file to determine CQ for.
            use_gpu: Whether to use GPU encoder (defaults to config.general.gpu).
            config: Config to use (defaults to self.config for backward compatibility).

        Returns:
            CQ quality value.
        """
        cfg = config if config is not None else self.config
        use_gpu = cfg.general.gpu if use_gpu is None else use_gpu
        encoder_args = select_encoder_args(cfg, use_gpu)
        default_cq = extract_quality_value(encoder_args)
        if default_cq is None:
            default_cq = 45 if use_gpu else 32
        if not file.metadata:
            return default_cq
        if file.metadata.custom_cq is not None:
            return file.metadata.custom_cq
        if not file.metadata.camera_model:
            return default_cq
        model = file.metadata.camera_model
        for key, rule in cfg.general.dynamic_quality.items():
            if key in model:
                return rule.cq
        return default_cq

    def _select_rate_config_for_file(
        self,
        file: VideoFile,
        cfg: AppConfig,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], str, str]:
        """Return effective rate config and source labels."""
        bps = cfg.general.bps
        minrate = cfg.general.minrate
        maxrate = cfg.general.maxrate
        rate_target_max_bps = cfg.general.rate_target_max_bps
        rate_source = "global"
        cap_source = "global" if rate_target_max_bps is not None else "none"

        if file.metadata and file.metadata.camera_model:
            model = file.metadata.camera_model
            for key, rule in cfg.general.dynamic_quality.items():
                if key in model and rule.rate is not None:
                    bps = rule.rate.bps
                    minrate = rule.rate.minrate
                    maxrate = rule.rate.maxrate
                    if rule.rate.rate_target_max_bps is not None:
                        rate_target_max_bps = rule.rate.rate_target_max_bps
                        cap_source = f"dynamic_quality:{key}"
                    rate_source = f"dynamic_quality:{key}"
                    break

        return bps, minrate, maxrate, rate_target_max_bps, rate_source, cap_source

    @staticmethod
    def _extract_encoder_codec(args: List[str]) -> str:
        for idx, arg in enumerate(args):
            stripped = str(arg).strip()
            if stripped == "-c:v" and idx + 1 < len(args):
                return str(args[idx + 1]).strip().lower()
            if stripped.startswith("-c:v "):
                return stripped.split(None, 1)[1].strip().lower()
        return "unknown"

    def _determine_rate_control(
        self,
        file: VideoFile,
        config: Optional[AppConfig] = None,
        use_gpu: Optional[bool] = None,
    ) -> ResolvedRateControl:
        cfg = config if config is not None else self.config
        use_gpu = cfg.general.gpu if use_gpu is None else use_gpu
        source_bps = None
        if file.metadata and file.metadata.bitrate_kbps and file.metadata.bitrate_kbps > 0:
            source_bps = file.metadata.bitrate_kbps * 1000.0

        bps, minrate, maxrate, rate_target_max_bps, rate_source, cap_source = self._select_rate_config_for_file(
            file, cfg
        )

        if cfg.general.debug:
            source_bitrate_text = (
                f"{int(round(source_bps))} bps ({format_bps_human(int(round(source_bps)))})"
                if source_bps is not None
                else "unavailable"
            )
            self.logger.info(
                f"RATE_CONFIG: {file.path.name} "
                f"source_bitrate={source_bitrate_text} "
                f"source={rate_source} bps={bps} minrate={minrate} maxrate={maxrate} "
                f"rate_target_max_bps={rate_target_max_bps}"
            )

        base_resolved = resolve_rate_control_values(
            bps,
            minrate,
            maxrate,
            source_bps,
        )
        resolved_target_bps = base_resolved.target_bps
        applied_target_bps = resolved_target_bps
        applied_minrate_bps = base_resolved.minrate_bps
        applied_maxrate_bps = base_resolved.maxrate_bps

        config_cap_bps = (
            parse_rate_cap_bps(rate_target_max_bps, field_name="rate_target_max_bps")
            if rate_target_max_bps is not None
            else None
        )
        encoder_args = select_encoder_args(cfg, use_gpu)
        encoder_codec = self._extract_encoder_codec(encoder_args)
        encoder_cap_bps = SVT_AV1_TARGET_MAX_BPS if encoder_codec == "libsvtav1" else None

        effective_cap_bps = None
        final_cap_source = None
        if config_cap_bps is not None and encoder_cap_bps is not None:
            if encoder_cap_bps <= config_cap_bps:
                effective_cap_bps = encoder_cap_bps
                final_cap_source = "encoder:libsvtav1_limit"
            else:
                effective_cap_bps = config_cap_bps
                final_cap_source = f"config:{cap_source}"
        elif encoder_cap_bps is not None:
            effective_cap_bps = encoder_cap_bps
            final_cap_source = "encoder:libsvtav1_limit"
        elif config_cap_bps is not None:
            effective_cap_bps = config_cap_bps
            final_cap_source = f"config:{cap_source}"

        if effective_cap_bps is not None:
            applied_target_bps = min(applied_target_bps, effective_cap_bps)
            if applied_maxrate_bps is not None:
                applied_maxrate_bps = min(applied_maxrate_bps, effective_cap_bps)

        if applied_minrate_bps is not None and applied_minrate_bps > applied_target_bps:
            applied_minrate_bps = applied_target_bps
        if applied_maxrate_bps is not None and applied_target_bps > applied_maxrate_bps:
            applied_target_bps = applied_maxrate_bps
        if (
            applied_minrate_bps is not None
            and applied_maxrate_bps is not None
            and applied_minrate_bps > applied_maxrate_bps
        ):
            applied_minrate_bps = applied_maxrate_bps

        applied_target_kbps = None
        if encoder_codec == "libsvtav1":
            applied_target_kbps = min(
                SVT_AV1_TARGET_MAX_KBPS,
                max(1, applied_target_bps // 1000),
            )
            applied_target_bps = applied_target_kbps * 1000
            if applied_minrate_bps is not None and applied_minrate_bps > applied_target_bps:
                applied_minrate_bps = applied_target_bps

        # "capped" means the resolved target exceeded an explicit cap (config or encoder).
        # Do not treat codec-specific rounding (e.g. SVT kbps quantization) as capping.
        was_capped = bool(
            effective_cap_bps is not None
            and resolved_target_bps > effective_cap_bps
        )
        source_bps_int = int(round(source_bps)) if source_bps is not None else None
        resolved = ResolvedRateControl(
            target_bps=applied_target_bps,
            minrate_bps=applied_minrate_bps,
            maxrate_bps=applied_maxrate_bps,
            resolved_target_bps=resolved_target_bps,
            config_cap_bps=config_cap_bps,
            encoder_cap_bps=encoder_cap_bps,
            effective_cap_bps=effective_cap_bps,
            applied_target_kbps=applied_target_kbps,
            was_capped=was_capped,
            cap_source=final_cap_source,
            source_bps=source_bps_int,
            target_expr=str(bps) if bps is not None else None,
            rate_source=rate_source,
        )

        if cfg.general.debug:
            resolved_text = (
                f"resolved_target={resolved_target_bps} bps ({format_bps_human(resolved_target_bps)}) "
                f"applied_target={resolved.target_bps} bps ({format_bps_human(resolved.target_bps)}) "
                f"effective_cap={resolved.effective_cap_bps if resolved.effective_cap_bps is not None else 'none'} "
                f"cap_source={resolved.cap_source or 'none'} "
                f"was_capped={resolved.was_capped} "
                f"minrate={resolved.minrate_bps if resolved.minrate_bps is not None else 'none'} "
                f"maxrate={resolved.maxrate_bps if resolved.maxrate_bps is not None else 'none'}"
            )
            if resolved.applied_target_kbps is not None:
                resolved_text += f" applied_target_kbps={resolved.applied_target_kbps}"
            self.logger.info(f"RATE_RESOLVED: {file.path.name} {resolved_text}")
            if resolved.was_capped:
                self.logger.info(
                    f"RATE_CAP_APPLIED: {file.path.name} "
                    f"resolved={resolved_target_bps} applied={resolved.target_bps} "
                    f"cap={resolved.effective_cap_bps} source={resolved.cap_source}"
                )

        return resolved

    def _quality_label_for_rate_tags(
        self,
        file: VideoFile,
        config: Optional[AppConfig] = None,
    ) -> str:
        """Return the configured rate target value to persist in VBCQuality tag."""
        cfg = config if config is not None else self.config
        bps, _, _, _, _, _ = self._select_rate_config_for_file(file, cfg)
        if bps is None:
            return "unknown"
        return str(bps)

    @staticmethod
    def _format_mbps_label_from_bps(bps: float) -> str:
        mbps = bps / 1_000_000.0
        value = f"{mbps:.3f}".rstrip("0").rstrip(".")
        return f"{value} Mbps"

    def _original_bitrate_label_for_tags(self, file: VideoFile) -> str:
        """Return source bitrate label used by VBCOriginalBitrate tag."""
        if file.metadata and file.metadata.bitrate_kbps and file.metadata.bitrate_kbps > 0:
            return self._format_mbps_label_from_bps(file.metadata.bitrate_kbps * 1000.0)
        return "unknown"

    def _quality_display_for_cq(self, cq_value: int, use_gpu: bool, config: Optional[AppConfig] = None) -> str:
        cfg = config if config is not None else self.config
        encoder_args = select_encoder_args(cfg, use_gpu)
        quality_flag = extract_quality_flag(encoder_args)
        quality_label = "CQ" if quality_flag == "-cq" else "CRF" if quality_flag == "-crf" else "Q"
        return f"{quality_label}{cq_value}"

    def _output_suffix_for_mode(self, use_gpu: Optional[bool] = None) -> str:
        """Return output file suffix (including dot) for the selected encoder."""
        use_gpu = self.config.general.gpu if use_gpu is None else use_gpu
        encoder_args = select_encoder_args(self.config, use_gpu)
        return output_extension_for_args(encoder_args)

    def _determine_rotation(self, file: VideoFile, config: Optional[AppConfig] = None) -> Optional[int]:
        """Determines if rotation is needed based on filename pattern.

        Args:
            file: Video file to determine rotation for.
            config: Config to use (defaults to self.config for backward compatibility).

        Returns:
            Rotation angle in degrees, or None if no rotation needed.
        """
        cfg = config if config is not None else self.config
        if cfg.general.manual_rotation is not None:
            return cfg.general.manual_rotation
        filename = file.path.name
        for pattern, angle in cfg.autorotate.patterns.items():
            if re.search(pattern, filename):
                return angle
        return None

    def _check_and_fix_color_space(
        self,
        input_path: Path,
        output_path: Path,
        stream_info: Dict[str, Any]
    ) -> tuple[Path, Optional[Path]]:
        """Fix reserved color space via remux when needed (legacy behavior)."""
        color_space = stream_info.get("color_space")
        codec_name = stream_info.get("codec")

        if color_space != "reserved":
            return input_path, None

        temp_fixed = output_path.parent / f"{output_path.stem}_colorfix.mp4"
        temp_fixed.parent.mkdir(parents=True, exist_ok=True)

        if codec_name == "hevc":
            bsf = "hevc_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1"
        elif codec_name == "h264":
            bsf = "h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1"
        else:
            self.logger.warning(f"Cannot fix color space for codec {codec_name}, proceeding with original file")
            return input_path, None

        try:
            fix_result = subprocess.run(
                [
                    "ffmpeg",
                    "-i", str(input_path),
                    "-c", "copy",
                    "-bsf:v", bsf,
                    str(temp_fixed),
                    "-y",
                    "-hide_banner",
                    "-loglevel", "error",
                ],
                capture_output=True,
                text=True,
                timeout=300
            )
        except subprocess.TimeoutExpired:
            self.logger.error(f"Timeout while fixing color space for {input_path.name}")
            if temp_fixed.exists():
                temp_fixed.unlink()
            return input_path, None

        if fix_result.returncode == 0 and temp_fixed.exists():
            self.logger.info(f"Successfully fixed color space for {input_path.name}")
            return temp_fixed, temp_fixed

        if temp_fixed.exists():
            temp_fixed.unlink()
        self.logger.warning(f"Failed to fix color space for {input_path.name}, proceeding with original file")
        return input_path, None

    @staticmethod
    def _rate_json_notes_for_tags(rate_control: Optional[ResolvedRateControl]) -> Optional[str]:
        if rate_control is None:
            return None
        payload = {
            "rate_control": {
                "mode": "rate",
                "target_expr": rate_control.target_expr,
                "source_bitrate_bps": rate_control.source_bps,
                "resolved_target_bps": rate_control.resolved_target_bps,
                "config_cap_bps": rate_control.config_cap_bps,
                "encoder_cap_bps": rate_control.encoder_cap_bps,
                "effective_cap_bps": rate_control.effective_cap_bps,
                "applied_target_bps": rate_control.target_bps,
                "applied_target_kbps": rate_control.applied_target_kbps,
                "was_capped": rate_control.was_capped,
                "cap_source": rate_control.cap_source,
                "rate_source": rate_control.rate_source,
            }
        }
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)

    def _build_vbc_tag_args(
        self,
        source_path: Path,
        quality_label: str,
        original_bitrate_label: str,
        encoder: str,
        original_size: int,
        finished_at: str,
        vbc_json_notes: Optional[str] = None,
    ) -> List[str]:
        tags = [
            f"-XMP:VBCOriginalName={source_path.name}",
            f"-XMP:VBCOriginalSize={original_size}",
            f"-XMP:VBCQuality={quality_label}",
            f"-XMP:VBCOriginalBitrate={original_bitrate_label}",
            f"-XMP:VBCEncoder={encoder}",
            f"-XMP:VBCFinishedAt={finished_at}",
        ]
        if vbc_json_notes:
            tags.append(f"-XMP:VBCJsonNotes={vbc_json_notes}")
        return tags

    def _copy_deep_metadata(
        self,
        source_path: Path,
        output_path: Path,
        err_path: Path,
        quality_label: str,
        original_bitrate_label: str,
        encoder: str,
        original_size: int,
        finished_at: str,
        vbc_json_notes: Optional[str] = None,
        record_error_marker: bool = True,
    ) -> None:
        """Copy full metadata from source to output using ExifTool (legacy behavior)."""
        config_path = Path(__file__).resolve().parents[2] / "conf" / "exiftool.conf"
        vbc_tags = self._build_vbc_tag_args(
            source_path,
            quality_label,
            original_bitrate_label,
            encoder,
            original_size,
            finished_at,
            vbc_json_notes=vbc_json_notes,
        )

        exiftool_cmd = ["exiftool"]
        if config_path.exists():
            exiftool_cmd.extend(["-config", str(config_path)])
        exiftool_cmd.extend([
            "-m",
            "-tagsFromFile", str(source_path),
            "-XMP:all", "-QuickTime:all", "-Keys:all", "-UserData:all",
            "-EXIF:all", "-GPS:all",
            "-XMP-exif:GPSLatitude<GPSLatitude",
            "-XMP-exif:GPSLongitude<GPSLongitude",
            "-XMP-exif:GPSAltitude<GPSAltitude",
            "-XMP-exif:GPSPosition<GPSPosition",
            "-QuickTime:GPSCoordinates<GPSPosition",
            "-Keys:GPSCoordinates<GPSPosition",
            # Fix for MTS/AVCHD missing dates in MP4
            "-QuickTime:CreateDate<DateTimeOriginal",
            "-QuickTime:ModifyDate<DateTimeOriginal",
            "-QuickTime:TrackCreateDate<DateTimeOriginal",
            "-QuickTime:TrackModifyDate<DateTimeOriginal",
            "-QuickTime:MediaCreateDate<DateTimeOriginal",
            "-QuickTime:MediaModifyDate<DateTimeOriginal",
            "-QuickTime:CreationDate<DateTimeOriginal",
            # Fix missing Make/Model
            "-QuickTime:Make<Make",
            "-QuickTime:Model<Model",
            "-UserData:Make<Make",
            "-UserData:Model<Model",
        ])
        if config_path.exists():
            exiftool_cmd.extend(vbc_tags)
        exiftool_cmd.extend([
            "-unsafe",
            "-overwrite_original",
            str(output_path)
        ])

        filename = source_path.name
        rate_bytes = 10 * 1024 * 1024  # 10 MiB/s
        size_bytes = None
        try:
            size_bytes = output_path.stat().st_size
        except OSError:
            try:
                size_bytes = source_path.stat().st_size
            except OSError:
                size_bytes = None
        if size_bytes is None:
            timeout_s = 30
        else:
            timeout_s = max(1, (size_bytes + rate_bytes - 1) // rate_bytes)

        if self.config.general.debug:
            self.logger.info(
                f"EXIF_COPY_TIMEOUT_SET: {filename} size_bytes={size_bytes} timeout={timeout_s}s"
            )
            max_attempts = 2
            timed_out = False
            for attempt in range(1, max_attempts + 1):
                try:
                    exif_start = time.monotonic()
                    remove_exiftool_tmp_for_target(output_path, self.logger)
                    self.logger.info(
                        f"EXIF_COPY_START: {filename} attempt {attempt}/{max_attempts}"
                    )
                    subprocess.run(
                        exiftool_cmd,
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=timeout_s
                    )
                    exif_elapsed = time.monotonic() - exif_start
                    self.logger.info(
                        f"EXIF_COPY_DONE: {filename} attempt {attempt}/{max_attempts} "
                        f"elapsed={exif_elapsed:.2f}s"
                    )
                    self.logger.info(f"Metadata copied successfully for {filename}")
                    timed_out = False
                    break
                except subprocess.TimeoutExpired:
                    timed_out = True
                    exif_elapsed = time.monotonic() - exif_start
                    self.logger.warning(
                        f"ExifTool metadata copy timed out after {timeout_s}s "
                        f"(attempt {attempt}/{max_attempts}) for {filename}"
                    )
                    self.logger.warning(
                        f"EXIF_COPY_TIMEOUT: {filename} attempt {attempt}/{max_attempts} "
                        f"elapsed={exif_elapsed:.2f}s"
                    )
                except subprocess.CalledProcessError as e:
                    exif_elapsed = time.monotonic() - exif_start
                    stderr = (e.stderr or "").strip()
                    stdout = (e.stdout or "").strip()
                    self.logger.warning(
                        f"EXIF_COPY_ERROR: {filename} attempt {attempt}/{max_attempts} "
                        f"elapsed={exif_elapsed:.2f}s returncode={e.returncode} "
                        f"stderr={stderr!r} stdout={stdout!r}"
                    )
                    self.logger.warning(f"Failed to copy deep metadata for {filename}: {e}")
                    timed_out = False
                    break
                except Exception as e:
                    exif_elapsed = time.monotonic() - exif_start
                    self.logger.warning(
                        f"EXIF_COPY_ERROR: {filename} attempt {attempt}/{max_attempts} "
                        f"elapsed={exif_elapsed:.2f}s error={e}"
                    )
                    self.logger.warning(f"Failed to copy deep metadata for {filename}: {e}")
                    timed_out = False
                    break
            if timed_out:
                if record_error_marker:
                    try:
                        err_path.write_text(
                            f"ExifTool metadata copy timed out after {timeout_s}s (2 attempts)."
                        )
                        self._record_session_error_marker(source_path, err_path)
                    except Exception:
                        pass
                self.logger.error(
                    f"ExifTool metadata copy timed out after {timeout_s}s (2 attempts) for {filename}"
                )
        else:
            try:
                remove_exiftool_tmp_for_target(output_path, self.logger)
                subprocess.run(
                    exiftool_cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=timeout_s,
                )
            except subprocess.TimeoutExpired:
                self.logger.warning(
                    f"ExifTool metadata copy timed out after {timeout_s}s for {filename}"
                )
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or "").strip()
                stdout = (e.stdout or "").strip()
                self.logger.warning(
                    f"Failed to copy deep metadata for {filename}: returncode={e.returncode} "
                    f"stderr={stderr!r} stdout={stdout!r}"
                )
            except Exception as e:
                self.logger.warning(f"Failed to copy deep metadata for {filename}: {e}")

    def _write_vbc_tags(
        self,
        source_path: Path,
        output_path: Path,
        quality_label: str,
        original_bitrate_label: str,
        encoder: str,
        original_size: int,
        finished_at: str,
        vbc_json_notes: Optional[str] = None,
    ) -> None:
        """Write VBC tags only (no metadata copy)."""
        config_path = Path(__file__).resolve().parents[2] / "conf" / "exiftool.conf"
        if not config_path.exists():
            self.logger.warning("ExifTool config not found; skipping VBC tags")
            return

        exiftool_cmd = [
            "exiftool",
            "-config", str(config_path),
            "-overwrite_original",
        ]
        exiftool_cmd.extend(
            self._build_vbc_tag_args(
                source_path,
                quality_label,
                original_bitrate_label,
                encoder,
                original_size,
                finished_at,
                vbc_json_notes=vbc_json_notes,
            )
        )
        exiftool_cmd.append(str(output_path))
        try:
            rate_bytes = 10 * 1024 * 1024  # 10 MiB/s
            try:
                size_bytes = output_path.stat().st_size
            except OSError:
                size_bytes = None
            if size_bytes is None:
                timeout_s = 30
            else:
                timeout_s = max(1, (size_bytes + rate_bytes - 1) // rate_bytes)
            remove_exiftool_tmp_for_target(output_path, self.logger)
            subprocess.run(
                exiftool_cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            self.logger.warning(
                f"ExifTool tag write timed out after {timeout_s}s for {output_path.name}"
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            stdout = (e.stdout or "").strip()
            self.logger.warning(
                f"Failed to write VBC tags for {output_path.name}: returncode={e.returncode} "
                f"stderr={stderr!r} stdout={stdout!r}"
            )
        except Exception as e:
            self.logger.warning(f"Failed to write VBC tags for {output_path.name}: {e}")

    @staticmethod
    def _normalize_exif_tag_name(tag_key: str) -> str:
        """Normalize ExifTool key names like 'XMP:VBCEncoder' for robust comparisons."""
        tail = str(tag_key).split(":")[-1]
        return re.sub(r"[^a-z0-9]", "", tail.lower())

    def _verify_output_file(
        self,
        output_path: Path,
        expected_video_frames: Optional[int] = None,
        max_dropped_frames: int = 0,
        require_vbc_tags: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        """Verify output by probing readability and checking required VBC EXIF tags."""
        if not output_path.exists():
            return False, "output file missing"

        try:
            self.ffprobe_adapter.get_stream_info(output_path)
        except Exception as exc:
            return False, f"ffprobe check failed: {exc}"

        if expected_video_frames is not None:
            try:
                # VBC's encoded MP4 has one video packet per output frame. Counting
                # packets verifies the muxed result without decoding the whole file.
                output_info = self.ffprobe_adapter.get_part_info(output_path)
                actual_video_frames = int(output_info.get("video_packets") or 0)
            except Exception as exc:
                return False, f"ffprobe frame-count check failed: {exc}"
            dropped_frames = expected_video_frames - actual_video_frames
            if dropped_frames < 0 or dropped_frames > max_dropped_frames:
                return False, (
                    "video frame count mismatch: "
                    f"expected {expected_video_frames}, got {actual_video_frames}"
                )
            if dropped_frames:
                self.logger.warning(
                    "OUTPUT_FRAME_LOSS_ACCEPTED: %s expected=%s actual=%s "
                    "dropped=%s limit=%s",
                    output_path,
                    expected_video_frames,
                    actual_video_frames,
                    dropped_frames,
                    max_dropped_frames,
                )

        if not require_vbc_tags:
            return True, None

        try:
            tags = self.exif_adapter.extract_tags(output_path)
        except Exception as exc:
            return False, f"ExifTool tag read failed: {exc}"

        normalized_keys = {self._normalize_exif_tag_name(k) for k in tags.keys()}
        missing = sorted(tag for tag in _REQUIRED_VBC_VERIFY_TAGS if tag not in normalized_keys)
        if missing:
            return False, f"missing VBC tags: {', '.join(missing)}"

        return True, None

    def _handle_verification_failure(self, message: str, action: str) -> None:
        """Handle verification failure according to configured action."""
        from vbc.domain.events import ActionMessage

        # Audible alert for detected bad compression (double bell helper).
        _emit_bell()

        normalized_action = str(action).strip().lower()
        if normalized_action == "pause":
            if not self._pause_requested:
                self._pause_requested = True
                self._pause_message = message
            self.event_bus.publish(ActionMessage(message=f"ERROR: verification failed, pausing queue ({message})"))
            return

        if normalized_action == "exit":
            with self._thread_lock:
                self._shutdown_requested = True
                self._thread_lock.notify_all()
            self._shutdown_event.set()
            self._verification_abort_message = message
            self._wait_event.set()
            self.event_bus.publish(ActionMessage(message=f"ERROR: verification failed, aborting ({message})"))
            return

        # Default: log and continue
        self.event_bus.publish(ActionMessage(message=f"Verification failed, continuing ({message})"))

    def _is_metadata_input_dir(self, input_dir: Path) -> bool:
        return input_dir in self._metadata_input_entries

    def _load_current_metadata_config(self) -> MetadataConfig:
        """Reload hot metadata overrides, retaining the last valid snapshot."""
        if self.config_path is None:
            return self._metadata_config.model_copy(deep=True)
        from vbc.config.loader import load_metadata_config

        with self._metadata_config_lock:
            try:
                self._metadata_config = load_metadata_config(self.config_path)
            except Exception as exc:
                self.logger.warning(
                    "Could not reload metadata config; using last valid values: %s",
                    exc,
                )
            return self._metadata_config.model_copy(deep=True)

    @staticmethod
    def _resolve_manifest_policies(
        manifest: CompressionManifest,
        metadata_config: MetadataConfig,
    ) -> tuple[str, str, str]:
        source_policy = metadata_config.source_policy or manifest.source_policy
        compression_profile = metadata_config.compression_profile or manifest.compression_profile
        return source_policy, compression_profile, metadata_config.audio_only

    @staticmethod
    def _next_backup_path(output_path: Path) -> Path:
        index = 1
        while True:
            candidate = output_path.with_name(f"{output_path.stem}_{index}{output_path.suffix}")
            if not candidate.exists():
                return candidate
            index += 1

    def _route_manifest_success(self, request: MetadataRequest) -> Path:
        request.success_dir.mkdir(parents=True, exist_ok=True)
        destination = request.success_dir / request.manifest_path.name
        request.manifest_path.replace(destination)
        self.logger.info("MANIFEST_DONE: %s -> %s", request.manifest_path, destination)
        return destination

    def _route_manifest_error(
        self,
        manifest_path: Path,
        error_dir: Path,
        message: str,
    ) -> Path:
        error_dir.mkdir(parents=True, exist_ok=True)
        destination = error_dir / manifest_path.name
        if manifest_path.exists():
            manifest_path.replace(destination)
        err_path = destination.with_suffix(".err")
        err_path.write_text(message)
        self.logger.error("MANIFEST_ERROR: %s (%s)", destination, message)
        return destination

    def _delete_manifest_sources(self, request: MetadataRequest) -> None:
        if request.source_policy != "delete_after_success":
            return
        for source_path in request.all_input_paths:
            if source_path.exists():
                source_path.unlink()
                self.logger.info("MANIFEST_SOURCE_DELETED: %s", source_path)

    def _discover_metadata_dir(
        self,
        input_dir: Path,
        success_dir: Path,
        error_dir: Path,
    ) -> tuple[List[VideoFile], Dict[str, Any]]:
        stats: Dict[str, Any] = {
            "files_found": 0,
            "files_to_process": 0,
            "already_compressed": 0,
            "ignored_small": 0,
            "ignored_err": 0,
            "ignored_err_entries": [],
        }
        candidates: List[VideoFile] = []
        metadata_config = self._load_current_metadata_config()

        for manifest_path in sorted(input_dir.rglob("*.json")):
            with self._manifest_inflight_lock:
                if manifest_path in self._manifest_inflight:
                    if self.config.general.debug:
                        self.logger.debug(
                            "MANIFEST_REFRESH_SKIP_INFLIGHT: %s",
                            manifest_path,
                        )
                    continue
            stats["files_found"] += 1
            try:
                manifest = CompressionManifest.model_validate_json(manifest_path.read_text())
                self.logger.info(
                    "MANIFEST_LOADED: json=%s request_id=%s producer=%s username=%s "
                    "recording_id=%s declared_size_bytes=%s declared_latest_mtime_ns=%s",
                    manifest_path,
                    manifest.request_id,
                    manifest.producer.app,
                    manifest.producer.username,
                    manifest.producer.recording_id,
                    manifest.producer.source_size_bytes,
                    manifest.producer.source_latest_mtime_ns,
                )
                source_policy, compression_profile, audio_only = self._resolve_manifest_policies(
                    manifest,
                    metadata_config,
                )
                output_path = Path(manifest.output_path)
                tmp_path = output_path.with_suffix(".tmp")
                if tmp_path.exists():
                    tmp_path.unlink()
                    self.logger.info("MANIFEST_STALE_TMP_REMOVED: %s", tmp_path)

                if output_path.exists():
                    output_ok, _ = self._verify_output_file(output_path)
                    if output_ok:
                        completed_request = MetadataRequest(
                            manifest_path=manifest_path,
                            metadata_dir=input_dir,
                            success_dir=success_dir,
                            error_dir=error_dir,
                            manifest=manifest,
                            parts=[],
                            source_policy=source_policy,
                            compression_profile=compression_profile,
                            audio_only=audio_only,
                            target_width=1,
                            target_height=1,
                        )
                        self._delete_manifest_sources(completed_request)
                        self._route_manifest_success(completed_request)
                        stats["already_compressed"] += 1
                        continue

                parts: List[MultipartPart] = []
                ignored_inputs: List[Path] = []
                for source_value in manifest.inputs:
                    source_path = Path(source_value)
                    if not source_path.is_file():
                        raise FileNotFoundError(f"Missing manifest input: {source_path}")
                    part_info = self.ffprobe_adapter.get_part_info(source_path)
                    video_packets = int(part_info.get("video_packets") or 0)
                    audio_packets = int(part_info.get("audio_packets") or 0)
                    has_usable_video = bool(part_info.get("has_video_stream")) and video_packets > 0
                    if not has_usable_video:
                        is_audio_only = bool(part_info.get("has_audio_stream")) and audio_packets > 0
                        if is_audio_only and audio_only == "ignore":
                            ignored_inputs.append(source_path)
                            self.logger.info(
                                "MANIFEST_AUDIO_ONLY_IGNORED: json=%s input=%s",
                                manifest_path,
                                source_path,
                            )
                            continue
                        reason = "audio-only input" if is_audio_only else "input has no video packets"
                        raise ValueError(f"Invalid manifest input ({reason}): {source_path}")

                    width = int(part_info.get("width") or 0)
                    height = int(part_info.get("height") or 0)
                    if width <= 0 or height <= 0:
                        raise ValueError(
                            f"Invalid video dimensions for {source_path}: {width}x{height}"
                        )
                    duration = float(part_info.get("duration") or 0.0)
                    if audio_packets == 0:
                        packet_duration = self.ffprobe_adapter.get_video_packet_duration(source_path)
                        duration = packet_duration or duration
                        if duration <= 0:
                            fps = float(part_info.get("fps") or 0.0)
                            duration = (1.0 / fps) if fps > 0 else 0.04
                    parts.append(
                        MultipartPart(
                            path=source_path,
                            width=width,
                            height=height,
                            codec=str(part_info.get("codec") or "unknown"),
                            audio_codec=part_info.get("audio_codec"),
                            fps=float(part_info.get("fps") or 0.0),
                            duration=max(0.0, duration),
                            bitrate_kbps=part_info.get("bitrate_kbps"),
                            color_space=part_info.get("color_space"),
                            pix_fmt=part_info.get("pix_fmt"),
                            video_packets=video_packets,
                            audio_packets=audio_packets,
                        )
                    )

                if not parts:
                    raise ValueError("Manifest has no usable video parts")
                orientations = {part.orientation for part in parts}
                if len(orientations) != 1:
                    raise ValueError(
                        "Manifest mixes incompatible video orientations: "
                        + ", ".join(sorted(orientations))
                    )

                target_part = max(parts, key=lambda part: part.width * part.height)
                target_width = target_part.width + (target_part.width % 2)
                target_height = target_part.height + (target_part.height % 2)
                total_size = sum(part.path.stat().st_size for part in parts)
                if total_size < self.file_scanner.min_size_bytes:
                    stats["ignored_small"] += 1
                    stats["files_found"] -= 1
                    continue

                first_part = parts[0]
                duration = sum(part.duration for part in parts)
                video_metadata = VideoMetadata(
                    width=target_width,
                    height=target_height,
                    codec=first_part.codec,
                    audio_codec=first_part.audio_codec or "no-audio",
                    fps=first_part.fps,
                    bitrate_kbps=first_part.bitrate_kbps,
                    megapixels=round(target_width * target_height / 1_000_000),
                    color_space=first_part.color_space,
                    pix_fmt=first_part.pix_fmt,
                    duration=duration,
                )
                request = MetadataRequest(
                    manifest_path=manifest_path,
                    metadata_dir=input_dir,
                    success_dir=success_dir,
                    error_dir=error_dir,
                    manifest=manifest,
                    parts=parts,
                    ignored_inputs=ignored_inputs,
                    source_policy=source_policy,
                    compression_profile=compression_profile,
                    audio_only=audio_only,
                    target_width=target_width,
                    target_height=target_height,
                )
                self.logger.info(
                    "MANIFEST_READY: json=%s output=%s parts=%s/%s size_bytes=%s "
                    "target=%sx%s source_policy=%s compression_profile=%s",
                    manifest_path,
                    output_path,
                    len(parts),
                    len(manifest.inputs),
                    total_size,
                    target_width,
                    target_height,
                    source_policy,
                    compression_profile,
                )
                candidates.append(
                    VideoFile(
                        path=output_path,
                        size_bytes=total_size,
                        metadata=video_metadata,
                        metadata_request=request,
                    )
                )
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                try:
                    destination = self._route_manifest_error(manifest_path, error_dir, message)
                except Exception as move_exc:
                    self.logger.exception("Failed to route manifest error for %s", manifest_path)
                    destination = manifest_path
                    message = f"{message}; failed to route manifest: {move_exc}"
                try:
                    size_bytes = destination.stat().st_size
                except OSError:
                    size_bytes = None
                stats["ignored_err"] += 1
                stats["files_found"] -= 1
                stats["ignored_err_entries"].append(
                    DiscoveryErrorEntry(
                        path=destination,
                        size_bytes=size_bytes,
                        error_message=message,
                    )
                )

        stats["files_to_process"] = len(candidates)
        return candidates, stats

    def _perform_discovery(self, input_dirs: Union[Path, List[Path]]) -> tuple:
        """Performs file discovery across multiple directories and returns (files_to_process, discovery_stats)."""
        import os

        input_dirs = self._normalize_input_dirs(input_dirs)
        all_files = []
        total_stats = {
            'files_found': 0,
            'files_to_process': 0,
            'already_compressed': 0,
            'ignored_small': 0,
            'ignored_err': 0,
            'ignored_err_entries': [],
        }

        for idx, input_dir in enumerate(input_dirs):
            output_dir = self._folder_mapping.get(input_dir)
            if output_dir is None:
                if self._use_output_dir_map_override:
                    output_dir = self._output_dir_map_override.get(input_dir)
                    if output_dir is None:
                        # New dir added at runtime — fall back to suffix
                        suffix = self.config.suffix_output_dirs
                        if not suffix:
                            raise ValueError(f"Output directory mapping missing for {input_dir}")
                        output_dir = input_dir.with_name(f"{input_dir.name}{suffix}")
                else:
                    output_dir = self._resolve_output_dir(input_dir, idx)
                self._folder_mapping[input_dir] = output_dir

            if self._is_metadata_input_dir(input_dir):
                errors_dir = self._get_errors_dir(input_dir)
                if errors_dir is None:
                    raise ValueError(f"Errors directory mapping missing for {input_dir}")
                metadata_files, metadata_stats = self._discover_metadata_dir(
                    input_dir,
                    output_dir,
                    errors_dir,
                )
                all_files.extend(metadata_files)
                for key in (
                    "files_found",
                    "already_compressed",
                    "ignored_small",
                    "ignored_err",
                ):
                    total_stats[key] += metadata_stats[key]
                total_stats["ignored_err_entries"].extend(
                    metadata_stats["ignored_err_entries"]
                )
                continue

            if self.config.general.debug:
                self.logger.info(f"DISCOVERY_START: scanning {input_dir}")

            # Single-pass discovery: collect stats and candidates in one walk
            folder_total_files = 0
            folder_ignored_small = 0
            folder_already_compressed = 0
            folder_ignored_err = 0
            folder_ignored_err_entries = []
            folder_files_to_process = []

            for root, dirs, filenames in os.walk(str(input_dir)):
                root_path = Path(root)
                if root_path.name.endswith("_out"):
                    dirs[:] = []
                    continue

                # Deterministic traversal
                dirs[:] = sorted(d for d in dirs if not d.endswith("_out"))
                filenames.sort()

                for fname in filenames:
                    fpath = root_path / fname
                    if fpath.suffix.lower() not in self.file_scanner.extensions:
                        continue

                    folder_total_files += 1

                    try:
                        file_stat = fpath.stat()
                    except OSError:
                        continue

                    if file_stat.st_size < self.file_scanner.min_size_bytes:
                        folder_ignored_small += 1
                        continue

                    try:
                        rel_path = fpath.relative_to(input_dir)
                    except ValueError:
                        rel_path = Path(fpath.name)
                    output_suffix = self._output_suffix_for_mode()
                    output_path = output_dir / rel_path.with_suffix(output_suffix)
                    err_path = output_path.with_suffix('.err')

                    # Check for error markers FIRST (before timestamp check)
                    if err_path.exists():
                        if self.config.general.clean_errors:
                            err_path.unlink()  # Remove error marker
                        else:
                            # Distinguish hw_cap errors from regular errors
                            try:
                                err_content = err_path.read_text()
                                if "Hardware is lacking required capabilities" in err_content:
                                    if self.config.general.cpu_fallback:
                                        err_path.unlink()
                                    else:
                                        # hw_cap is not counted as ignored_err
                                        continue
                                else:
                                    folder_ignored_err += 1
                                    folder_ignored_err_entries.append(
                                        DiscoveryErrorEntry(
                                            path=fpath,
                                            size_bytes=file_stat.st_size,
                                            error_message=(err_content.strip() or "Error marker present"),
                                        )
                                    )
                            except (OSError, UnicodeDecodeError):
                                folder_ignored_err += 1
                                folder_ignored_err_entries.append(
                                    DiscoveryErrorEntry(
                                        path=fpath,
                                        size_bytes=file_stat.st_size,
                                        error_message="Unreadable .err marker",
                                    )
                                )
                            if err_path.exists():
                                continue

                    # Check if already compressed
                    if output_path.exists() and output_path.stat().st_mtime >= file_stat.st_mtime:
                        folder_already_compressed += 1
                        continue

                    # AV1 check is done during processing, not discovery
                    folder_files_to_process.append(
                        VideoFile(path=fpath, size_bytes=file_stat.st_size)
                    )

            # Aggregate stats
            # files_found = only files that could be processed (exclude ignored_small, ignored_err)
            total_stats['files_found'] += (folder_total_files - folder_ignored_small - folder_ignored_err)
            total_stats['already_compressed'] += folder_already_compressed
            total_stats['ignored_small'] += folder_ignored_small
            total_stats['ignored_err'] += folder_ignored_err
            total_stats['ignored_err_entries'].extend(folder_ignored_err_entries)

            all_files.extend(folder_files_to_process)

            if self.config.general.debug:
                self.logger.info(
                    f"DISCOVERY_END ({input_dir.name}): found={folder_total_files}, to_process={len(folder_files_to_process)}, "
                    f"already_compressed={folder_already_compressed}, ignored_small={folder_ignored_small}, ignored_err={folder_ignored_err}"
                )

        # Sort all files by filename for deterministic processing order
        all_files = sort_files(all_files, input_dirs, self.config.general, self.file_scanner.extensions)

        # Update final stats
        total_stats['files_to_process'] = len(all_files)

        if self.config.general.debug:
            self.logger.info(
                f"DISCOVERY_END (all folders): found={total_stats['files_found']}, to_process={len(all_files)}, "
                f"already_compressed={total_stats['already_compressed']}, ignored_small={total_stats['ignored_small']}, ignored_err={total_stats['ignored_err']}"
            )

        # Build local config registry (scan for VBC.YAML files)
        if self.local_registry:
            self.local_registry.build_from_discovery(input_dirs)

        return all_files, total_stats

    def _trigger_critical_shutdown(self, reason: str):
        """Initiate immediate shutdown due to critical error (e.g. disk full/IO error)."""
        self.logger.error(f"CRITICAL SHUTDOWN: {reason}")
        with self._thread_lock:
            self._shutdown_requested = True
            self._thread_lock.notify_all()
        self._shutdown_event.set()
        from vbc.domain.events import ActionMessage
        self.event_bus.publish(ActionMessage(message=f"CRITICAL ERROR: {reason}"))

    def _move_completed_file(self, video_file: VideoFile, output_dir: Path) -> bool:
        """Move already encoded file to output directory safely."""
        source_path = video_file.path
        
        def _hash_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
            hasher = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(chunk_size), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()

        try:
            rel_path = video_file.path.name # Simple name for now, or relative logic
            # Try to get relative path if possible
            try:
                # Find which input dir it belongs to
                input_dir = self._find_input_folder(source_path)
                if input_dir:
                    rel_path = source_path.relative_to(input_dir)
            except ValueError:
                pass
                
            dest_path = output_dir / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            # Check if destination exists
            if dest_path.exists():
                src_size = source_path.stat().st_size
                dest_size = dest_path.stat().st_size
                
                if src_size == dest_size:
                    try:
                        src_hash = _hash_file(source_path)
                        dest_hash = _hash_file(dest_path)
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to hash files for duplicate check ({source_path.name}): {e}"
                        )
                        src_hash = None
                        dest_hash = None

                    if src_hash is not None and src_hash == dest_hash:
                        # Identical file exists in destination. Safe to delete source.
                        self.logger.info(
                            f"Duplicate found in output (hash match). Deleting source: {source_path}"
                        )
                        source_path.unlink()
                        return True
                    # Same size but different hash or hashing failed - treat as different file.
                    stem = dest_path.stem
                    suffix = dest_path.suffix
                    dest_path = dest_path.with_name(f"{stem}_vbc_dup{suffix}")
                    self.logger.warning(
                        f"Destination exists with same size but different content. Renaming move to: {dest_path.name}"
                    )
                else:
                    # Different file exists. Rename to avoid overwrite.
                    stem = dest_path.stem
                    suffix = dest_path.suffix
                    dest_path = dest_path.with_name(f"{stem}_vbc_dup{suffix}")
                    self.logger.warning(f"Destination exists with different size. Renaming move to: {dest_path.name}")

            # Perform Move
            self.logger.info(f"Moving already encoded file: {source_path.name} -> {dest_path}")
            shutil.move(str(source_path), str(dest_path))
            
            # Verify Move
            if not dest_path.exists():
                raise RuntimeError(f"Move failed: Destination {dest_path} not found after move")
            
            if dest_path.stat().st_size != video_file.size_bytes:
                 raise RuntimeError(f"Move failed: Size mismatch (src={video_file.size_bytes}, dest={dest_path.stat().st_size})")

            return True

        except Exception as e:
            msg = f"Failed to move file {source_path.name}: {str(e)}"
            self._trigger_critical_shutdown(msg)
            raise RuntimeError(msg)

    def _fail_metadata_request(
        self,
        video_file: VideoFile,
        message: str,
        job: Optional[CompressionJob] = None,
        publish: bool = True,
    ) -> None:
        request = video_file.metadata_request
        if request is None:
            return
        failed_job = job or CompressionJob(
            source_file=video_file,
            status=JobStatus.FAILED,
            output_path=video_file.path,
        )
        failed_job.status = JobStatus.FAILED
        failed_job.error_message = message
        try:
            self._route_manifest_error(request.manifest_path, request.error_dir, message)
        except Exception as exc:
            self.logger.exception("Failed to route manifest after job error: %s", exc)
        if publish:
            self.event_bus.publish(JobFailed(job=failed_job, error_message=message))

    def _process_metadata_request(self, video_file: VideoFile) -> None:
        request = video_file.metadata_request
        if request is None:
            return
        filename = video_file.path.name
        job: Optional[CompressionJob] = None

        try:
            metadata_config = self._load_current_metadata_config()
            source_policy, compression_profile, audio_only = self._resolve_manifest_policies(
                request.manifest,
                metadata_config,
            )
            request.source_policy = source_policy
            request.compression_profile = compression_profile
            request.audio_only = audio_only

            if request.ignored_inputs and audio_only == "fail":
                ignored = ", ".join(str(path) for path in request.ignored_inputs)
                self._fail_metadata_request(
                    video_file,
                    f"Manifest contains audio-only input(s): {ignored}",
                )
                return

            output_path = Path(request.manifest.output_path)
            tmp_path = output_path.with_suffix(".tmp")
            if tmp_path.exists():
                tmp_path.unlink()
                self.logger.info("MANIFEST_STALE_TMP_REMOVED: %s", tmp_path)

            if output_path.exists():
                output_ok, _ = self._verify_output_file(output_path)
                if output_ok:
                    self._delete_manifest_sources(request)
                    self._route_manifest_success(request)
                    job = CompressionJob(
                        source_file=video_file,
                        status=JobStatus.COMPLETED,
                        output_path=output_path,
                        output_size_bytes=output_path.stat().st_size,
                        duration_seconds=0.0,
                        verification_passed=True,
                    )
                    self.event_bus.publish(JobCompleted(job=job))
                    return

            for source_path in request.all_input_paths:
                if not source_path.is_file():
                    self._fail_metadata_request(
                        video_file,
                        f"Missing manifest input: {source_path}",
                    )
                    return

            if output_path.exists():
                backup_path = self._next_backup_path(output_path)
                output_path.rename(backup_path)
                self.logger.warning(
                    "MANIFEST_OUTPUT_BACKUP: untagged output %s -> %s",
                    output_path,
                    backup_path,
                )
            output_path.parent.mkdir(parents=True, exist_ok=True)

            job_config = self.config.model_copy(deep=True)
            profile_rule = job_config.general.dynamic_quality.get(compression_profile)
            if profile_rule is None:
                job_config.general.dynamic_quality = {}
                if video_file.metadata:
                    video_file.metadata.custom_cq = None
                    video_file.metadata.camera_model = None
                self.logger.info(
                    "MANIFEST_PROFILE_DEFAULT: json=%s profile=%s",
                    request.manifest_path,
                    compression_profile,
                )
            else:
                job_config.general.dynamic_quality = {compression_profile: profile_rule}
                if video_file.metadata:
                    video_file.metadata.custom_cq = profile_rule.cq
                    video_file.metadata.camera_model = compression_profile

            use_gpu = job_config.general.gpu
            rotation = self._determine_rotation(video_file, config=job_config)
            quality_value: Optional[int] = None
            rate_control: Optional[ResolvedRateControl] = None
            vbc_json_notes: Optional[str] = None
            if job_config.general.quality_mode == "rate":
                rate_control = self._determine_rate_control(
                    video_file,
                    config=job_config,
                    use_gpu=use_gpu,
                )
                quality_display = format_bps_human(rate_control.target_bps)
                quality_tag_label = self._quality_label_for_rate_tags(
                    video_file,
                    config=job_config,
                )
                vbc_json_notes = self._rate_json_notes_for_tags(rate_control)
            else:
                quality_value = self._determine_cq(
                    video_file,
                    use_gpu=use_gpu,
                    config=job_config,
                )
                quality_display = self._quality_display_for_cq(
                    quality_value,
                    use_gpu=use_gpu,
                    config=job_config,
                )
                quality_tag_label = quality_display

            job = CompressionJob(
                source_file=video_file,
                output_path=output_path,
                rotation_angle=rotation or 0,
                quality_value=quality_value,
                quality_display=quality_display,
            )
            self.event_bus.publish(JobStarted(job=job))
            job.status = JobStatus.PROCESSING
            self.ffmpeg_adapter.compress(
                job,
                job_config,
                use_gpu,
                quality=quality_value,
                rate_control=rate_control,
                rotate=rotation,
                shutdown_event=self._shutdown_event,
            )

            if (
                job.status == JobStatus.HW_CAP_LIMIT
                and job_config.general.cpu_fallback
                and use_gpu
            ):
                self.logger.info("FFMPEG_FALLBACK: %s (manifest hw_cap -> CPU)", filename)
                use_gpu = False
                if job_config.general.quality_mode == "rate":
                    rate_control = self._determine_rate_control(
                        video_file,
                        config=job_config,
                        use_gpu=False,
                    )
                    quality_value = None
                    quality_display = format_bps_human(rate_control.target_bps)
                    quality_tag_label = self._quality_label_for_rate_tags(
                        video_file,
                        config=job_config,
                    )
                    vbc_json_notes = self._rate_json_notes_for_tags(rate_control)
                else:
                    quality_value = self._determine_cq(
                        video_file,
                        use_gpu=False,
                        config=job_config,
                    )
                    quality_display = self._quality_display_for_cq(
                        quality_value,
                        use_gpu=False,
                        config=job_config,
                    )
                    quality_tag_label = quality_display
                job.status = JobStatus.PROCESSING
                job.error_message = None
                job.quality_value = quality_value
                job.quality_display = quality_display
                self.event_bus.publish(JobStarted(job=job))
                self.ffmpeg_adapter.compress(
                    job,
                    job_config,
                    False,
                    quality=quality_value,
                    rate_control=rate_control,
                    rotate=rotation,
                    shutdown_event=self._shutdown_event,
                )

            if job.status == JobStatus.INTERRUPTED:
                self.event_bus.publish(
                    JobFailed(job=job, error_message=job.error_message or "Interrupted")
                )
                return
            if job.status != JobStatus.COMPLETED or job.output_path is None:
                self._fail_metadata_request(
                    video_file,
                    job.error_message or "Compression failed",
                    job=job,
                    publish=False,
                )
                return

            expected_video_frames = job.expected_video_frames
            if expected_video_frames is None:
                self.logger.warning(
                    "FFMPEG_FRAME_COUNT_FALLBACK: %s",
                    filename,
                )
                expected_video_frames = sum(
                    self.ffprobe_adapter.get_video_frame_count(
                        part.path,
                        shutdown_event=self._shutdown_event,
                    )
                    for part in request.parts
                )

            verify_ok, verify_error = self._verify_output_file(
                job.output_path,
                expected_video_frames=expected_video_frames,
                max_dropped_frames=metadata_config.max_dropped_frames,
                require_vbc_tags=False,
            )
            if not verify_ok:
                message = f"Verification failed: {verify_error or 'unknown verification error'}"
                self._fail_metadata_request(video_file, message, job=job)
                if job_config.general.verify_fail_action != "false":
                    self._handle_verification_failure(
                        message,
                        job_config.general.verify_fail_action,
                    )
                return

            source_path = request.parts[0].path
            encoder_args = select_encoder_args(job_config, use_gpu)
            encoder_label = infer_encoder_label(encoder_args, use_gpu)
            finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            original_bitrate_label = self._original_bitrate_label_for_tags(video_file)
            tag_err_path = request.error_dir / request.manifest_path.with_suffix(".err").name
            if job_config.general.copy_metadata:
                self._copy_deep_metadata(
                    source_path,
                    job.output_path,
                    tag_err_path,
                    quality_tag_label,
                    original_bitrate_label,
                    encoder_label,
                    video_file.size_bytes,
                    finished_at,
                    vbc_json_notes=vbc_json_notes,
                    record_error_marker=False,
                )
            else:
                self._write_vbc_tags(
                    source_path,
                    job.output_path,
                    quality_tag_label,
                    original_bitrate_label,
                    encoder_label,
                    video_file.size_bytes,
                    finished_at,
                    vbc_json_notes=vbc_json_notes,
                )

            verify_ok, verify_error = self._verify_output_file(
                job.output_path,
            )
            if not verify_ok:
                message = f"Verification failed: {verify_error or 'unknown verification error'}"
                self._fail_metadata_request(video_file, message, job=job)
                if job_config.general.verify_fail_action != "false":
                    self._handle_verification_failure(
                        message,
                        job_config.general.verify_fail_action,
                    )
                return

            job.verification_passed = True
            job.output_size_bytes = job.output_path.stat().st_size
            self._delete_manifest_sources(request)
            self._route_manifest_success(request)
            self.event_bus.publish(JobCompleted(job=job))
        except InterruptedError:
            self.logger.info("MANIFEST_FRAME_SCAN_INTERRUPTED: %s", filename)
            return
        except Exception as exc:
            if job is not None and job.status == JobStatus.INTERRUPTED:
                return
            self._fail_metadata_request(
                video_file,
                f"Exception: {exc}",
                job=job,
            )

    def _process_file(self, video_file: VideoFile, input_dir: Optional[Path] = None):
        """Processes a single file with dynamic concurrency control."""
        filename = video_file.path.name
        start_time = time.monotonic() if self.config.general.debug else None

        if self.config.general.debug:
            thread_id = threading.get_ident()
            self.logger.info(f"PROCESS_START: {filename} (thread {thread_id})")

        with self._thread_lock:
            while self._active_threads >= self._current_max_threads:
                self._thread_lock.wait()

            if self._shutdown_requested:
                if self.config.general.debug:
                    self.logger.info(f"PROCESS_SKIP: {filename} (shutdown)")
                return

            self._active_threads += 1

        if video_file.metadata_request is not None:
            manifest_path = video_file.metadata_request.manifest_path
            with self._manifest_inflight_lock:
                self._manifest_inflight.add(manifest_path)
            try:
                self._process_metadata_request(video_file)
            finally:
                with self._manifest_inflight_lock:
                    self._manifest_inflight.discard(manifest_path)
                with self._thread_lock:
                    self._active_threads -= 1
                    self._thread_lock.notify_all()
            return

        job = None
        err_path = None
        temp_fixed_file = None
        input_path = video_file.path
        stream_info = None

        try:
            # Find which input folder contains this file
            if input_dir is None:
                input_dir = self._find_input_folder(video_file.path)
            if not input_dir:
                self.logger.error(f"Cannot determine input folder for {video_file.path}")
                return
            output_dir = self._folder_mapping.get(input_dir)
            if output_dir is None:
                output_dir = self._get_output_dir(input_dir)
                self._folder_mapping[input_dir] = output_dir

            try:
                rel_path = video_file.path.relative_to(input_dir)
            except ValueError:
                rel_path = Path(video_file.path.name)

            use_gpu = self.config.general.gpu
            output_suffix = self._output_suffix_for_mode(use_gpu)
            output_path = output_dir / rel_path.with_suffix(output_suffix)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            err_path = output_path.with_suffix('.err')

            if err_path.exists():
                if self.config.general.clean_errors:
                    err_path.unlink()
                else:
                    self.event_bus.publish(JobFailed(job=CompressionJob(source_file=video_file, status=JobStatus.SKIPPED), error_message="Existing error marker found"))
                    return

            try:
                stream_info = self.ffprobe_adapter.get_stream_info(video_file.path)
            except Exception as e:
                err_msg = "File is corrupted (ffprobe failed to read). Skipped."
                try:
                    self._write_job_error_marker(video_file, err_path, err_msg)
                except Exception:
                    pass
                self.logger.error(f"Corrupted file detected (ffprobe failed): {filename} - {e}")
                job = CompressionJob(source_file=video_file, status=JobStatus.FAILED, output_path=output_path, error_message=err_msg)
                self.event_bus.publish(JobFailed(job=job, error_message=err_msg))
                return

            # Reject files with invalid source video dimensions early.
            try:
                src_width = int(stream_info.get("width") or 0)
                src_height = int(stream_info.get("height") or 0)
            except (TypeError, ValueError):
                src_width = 0
                src_height = 0
            if src_width <= 0 or src_height <= 0:
                err_msg = (
                    f"Invalid source video dimensions from ffprobe: "
                    f"width={src_width}, height={src_height}. Skipped."
                )
                try:
                    self._write_job_error_marker(video_file, err_path, err_msg)
                except Exception:
                    pass
                self.logger.error(f"Corrupted file detected (invalid dimensions): {filename} - {err_msg}")
                job = CompressionJob(
                    source_file=video_file,
                    status=JobStatus.FAILED,
                    output_path=output_path,
                    error_message=err_msg,
                )
                self.event_bus.publish(JobFailed(job=job, error_message=err_msg))
                return

            input_path, temp_fixed_file = self._check_and_fix_color_space(video_file.path, output_path, stream_info)

            # 1. Metadata & Decision (using thread-safe cache)
            video_file.metadata = self._get_metadata(video_file, base_metadata=stream_info)

            if video_file.metadata and video_file.metadata.vbc_encoded:
                # File is already encoded by VBC but is in input folder.
                # Move it to output folder safely.
                if self._move_completed_file(video_file, output_dir):
                    with self._stats_lock:
                        self.skipped_vbc_count += 1
                    # Publish as Completed (Done) since it's effectively finished work
                    # We fake the job object for the event
                    job = CompressionJob(source_file=video_file, status=JobStatus.COMPLETED, output_path=output_path, output_size_bytes=video_file.size_bytes, duration_seconds=0.1)
                    # Note: output_path might differ if renamed, but for event log it's fine
                    self.event_bus.publish(JobCompleted(job=job))
                    return

            if self.config.general.skip_av1 and video_file.metadata and "av1" in video_file.metadata.codec.lower():
                self.event_bus.publish(JobFailed(job=CompressionJob(source_file=video_file, status=JobStatus.SKIPPED), error_message="Already encoded in AV1"))
                return

            if self.config.general.filter_cameras:
                cam_model = ""
                if video_file.metadata:
                    cam_model = video_file.metadata.camera_model or video_file.metadata.camera_raw or ""
                matched = False
                for filter_pattern in self.config.general.filter_cameras:
                    if filter_pattern.lower() in cam_model.lower():
                        matched = True
                        break
                if not matched:
                    self.event_bus.publish(JobFailed(job=CompressionJob(source_file=video_file, status=JobStatus.SKIPPED), error_message=f'Camera model "{cam_model}" not in filter'))
                    return

            # Determine job-specific config and source
            from vbc.config.overrides import build_job_config
            job_config, config_source = build_job_config(
                self.config,           # base global config
                self.local_registry,   # local VBC.YAML registry
                video_file.path,       # file being processed
                self.cli_overrides     # CLI overrides
            )

            rotation = self._determine_rotation(video_file, config=job_config)
            quality_value: Optional[int] = None
            quality_display = "unknown"
            quality_tag_label = "unknown"
            original_bitrate_label = self._original_bitrate_label_for_tags(video_file)
            rate_control: Optional[ResolvedRateControl] = None
            vbc_json_notes: Optional[str] = None

            try:
                if job_config.general.quality_mode == "rate":
                    rate_control = self._determine_rate_control(
                        video_file,
                        config=job_config,
                        use_gpu=use_gpu,
                    )
                    quality_display = format_bps_human(rate_control.target_bps)
                    quality_tag_label = self._quality_label_for_rate_tags(
                        video_file,
                        config=job_config,
                    )
                    vbc_json_notes = self._rate_json_notes_for_tags(rate_control)
                else:
                    quality_value = self._determine_cq(video_file, use_gpu=use_gpu, config=job_config)
                    quality_display = self._quality_display_for_cq(
                        quality_value,
                        use_gpu=use_gpu,
                        config=job_config,
                    )
                    quality_tag_label = quality_display
            except ValueError as exc:
                err_msg = str(exc)
                failed_job = CompressionJob(
                    source_file=video_file,
                    status=JobStatus.FAILED,
                    output_path=output_path,
                    error_message=err_msg,
                    config_source=config_source,
                )
                self._write_job_error_marker(video_file, err_path, err_msg)
                self.event_bus.publish(JobFailed(job=failed_job, error_message=err_msg))
                return

            job = CompressionJob(
                source_file=video_file,
                output_path=output_path,
                rotation_angle=rotation or 0,
                quality_value=quality_value,
                quality_display=quality_display,
                config_source=config_source
            )

            # 2. Compress
            self.event_bus.publish(JobStarted(job=job))
            job.status = JobStatus.PROCESSING
            self.ffmpeg_adapter.compress(
                job,
                job_config,
                use_gpu,
                quality=quality_value,
                rate_control=rate_control,
                rotate=rotation,
                shutdown_event=self._shutdown_event,
                input_path=input_path,
            )
            if (
                job.status == JobStatus.HW_CAP_LIMIT
                and job_config.general.cpu_fallback
                and use_gpu
            ):
                self.logger.info(f"FFMPEG_FALLBACK: {filename} (hw_cap -> CPU)")
                use_gpu = False
                if job_config.general.quality_mode == "rate":
                    rate_control = self._determine_rate_control(
                        video_file,
                        config=job_config,
                        use_gpu=False,
                    )
                    quality_value = None
                    quality_display = format_bps_human(rate_control.target_bps)
                    quality_tag_label = self._quality_label_for_rate_tags(
                        video_file,
                        config=job_config,
                    )
                    vbc_json_notes = self._rate_json_notes_for_tags(rate_control)
                else:
                    quality_value = self._determine_cq(video_file, use_gpu=False, config=job_config)
                    quality_display = self._quality_display_for_cq(
                        quality_value,
                        use_gpu=False,
                        config=job_config,
                    )
                    quality_tag_label = quality_display
                output_suffix = self._output_suffix_for_mode(use_gpu=False)
                output_path_cpu = output_dir / rel_path.with_suffix(output_suffix)
                output_path_cpu.parent.mkdir(parents=True, exist_ok=True)
                if output_path_cpu != job.output_path:
                    job.output_path = output_path_cpu
                    err_path = output_path_cpu.with_suffix('.err')
                job.status = JobStatus.PROCESSING
                job.error_message = None
                job.quality_value = quality_value
                job.quality_display = quality_display
                self.event_bus.publish(JobStarted(job=job))
                self.ffmpeg_adapter.compress(
                    job,
                    job_config,
                    use_gpu,
                    quality=quality_value,
                    rate_control=rate_control,
                    rotate=rotation,
                    shutdown_event=self._shutdown_event,
                    input_path=input_path,
                )

            # Check final status after compression
            if job.status == JobStatus.COMPLETED:
                kept_original = False
                if job.output_path.exists():
                    out_size = job.output_path.stat().st_size
                    in_size = video_file.size_bytes
                    ratio = out_size / in_size
                    kept_original = ratio > (1.0 - job_config.general.min_compression_ratio)
                    if kept_original:
                        shutil.copy2(video_file.path, job.output_path)
                        job.error_message = f"Ratio {ratio:.2f} above threshold, kept original: {filename}"
                        self.logger.info(
                            f"MIN_RATIO_SKIP: {filename} ratio={ratio:.2f} "
                            f"threshold={job_config.general.min_compression_ratio:.2f} kept_original=True"
                        )
                    else:
                        encoder_args = select_encoder_args(job_config, use_gpu)
                        encoder_label = infer_encoder_label(encoder_args, use_gpu)
                        finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
                        quality_label = quality_tag_label
                        if job_config.general.copy_metadata:
                            self._copy_deep_metadata(
                                video_file.path,
                                job.output_path,
                                err_path,
                                quality_label,
                                original_bitrate_label,
                                encoder_label,
                                video_file.size_bytes,
                                finished_at,
                                vbc_json_notes=vbc_json_notes,
                            )
                        else:
                            self._write_vbc_tags(
                                video_file.path,
                                job.output_path,
                                quality_label,
                                original_bitrate_label,
                                encoder_label,
                                video_file.size_bytes,
                                finished_at,
                                vbc_json_notes=vbc_json_notes,
                            )

                    if (
                        job_config.general.debug
                        and job_config.general.quality_mode == "rate"
                        and not kept_original
                    ):
                        try:
                            output_info = self.ffprobe_adapter.get_stream_info(job.output_path)
                            output_bitrate_kbps = output_info.get("bitrate_kbps")
                            if output_bitrate_kbps and output_bitrate_kbps > 0:
                                output_bps = int(round(output_bitrate_kbps * 1000))
                                self.logger.info(
                                    f"RATE_OUTPUT: {filename} "
                                    f"final_bitrate={output_bps} bps ({format_bps_human(output_bps)})"
                                )
                            else:
                                self.logger.info(
                                    f"RATE_OUTPUT: {filename} final_bitrate=unavailable"
                                )
                        except Exception as exc:
                            self.logger.warning(
                                f"RATE_OUTPUT: {filename} failed to probe output bitrate: {exc}"
                            )

                if (
                    job_config.general.verify_fail_action != "false"
                    and not kept_original
                    and job.output_path is not None
                ):
                    self.logger.info(f"VERIFY_START: {filename}")
                    verify_ok, verify_error = self._verify_output_file(job.output_path)
                    if verify_ok:
                        job.verification_passed = True
                        self.logger.info(f"VERIFY_OK: {filename}")
                    else:
                        details = verify_error or "unknown verification error"
                        job.status = JobStatus.FAILED
                        job.verification_passed = False
                        job.verification_error = details
                        job.error_message = f"Verification failed: {details}"
                        self.logger.error(f"VERIFY_FAIL: {filename} ({details})")
                        try:
                            self._write_job_error_marker(video_file, err_path, job.error_message)
                        except Exception:
                            pass
                        self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
                        self._handle_verification_failure(
                            job.error_message,
                            job_config.general.verify_fail_action,
                        )
                        if self.config.general.debug and start_time:
                            elapsed = time.monotonic() - start_time
                            self.logger.info(
                                f"PROCESS_END: {filename} status=failed reason=verification elapsed={elapsed:.2f}s"
                            )
                        return

                self.event_bus.publish(JobCompleted(job=job))
                if self.config.general.debug and start_time:
                    elapsed = time.monotonic() - start_time
                    self.logger.info(f"PROCESS_END: {filename} status=completed elapsed={elapsed:.2f}s")
            elif job.status == JobStatus.INTERRUPTED:
                # User pressed Ctrl+C - don't create .err, already cleaned up by FFmpegAdapter
                self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
                if self.config.general.debug and start_time:
                    elapsed = time.monotonic() - start_time
                    self.logger.info(f"PROCESS_END: {filename} status=interrupted elapsed={elapsed:.2f}s")
            elif job.status in (JobStatus.HW_CAP_LIMIT, JobStatus.FAILED):
                # Event already published by FFmpeg adapter, just write error marker
                self._write_job_error_marker(video_file, err_path, job.error_message or "Unknown error")
                if self.config.general.debug and start_time:
                    elapsed = time.monotonic() - start_time
                    self.logger.info(f"PROCESS_END: {filename} status={job.status.value} elapsed={elapsed:.2f}s")
            elif job.status == JobStatus.PROCESSING:
                # Status not updated - treat as unknown error
                job.status = JobStatus.FAILED
                job.error_message = "Compression finished but status not updated"
                self._write_job_error_marker(video_file, err_path, job.error_message)
                self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
                if self.config.general.debug and start_time:
                    elapsed = time.monotonic() - start_time
                    self.logger.info(f"PROCESS_END: {filename} status=failed reason=status_not_updated elapsed={elapsed:.2f}s")

        except KeyboardInterrupt:
            # Ctrl+C during processing - already handled by FFmpegAdapter if during ffmpeg
            # If happens elsewhere, set INTERRUPTED status
            if job and job.status == JobStatus.PROCESSING:
                job.status = JobStatus.INTERRUPTED
                job.error_message = "Interrupted by user (Ctrl+C)"
            if job:
                self.event_bus.publish(JobFailed(job=job, error_message=job.error_message or "Interrupted"))
            if self.config.general.debug and start_time:
                elapsed = time.monotonic() - start_time
                self.logger.info(f"PROCESS_END: {filename} status=interrupted elapsed={elapsed:.2f}s")
            # Re-raise to propagate to main loop
            raise
        except Exception as e:
            # Log exception but don't crash the thread
            self.logger.error(f"Exception processing {filename}: {e}")
            if job:
                job.status = JobStatus.FAILED
                job.error_message = f"Exception: {str(e)}"
                if err_path:
                    self._write_job_error_marker(video_file, err_path, job.error_message)
                self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
            if self.config.general.debug and start_time:
                elapsed = time.monotonic() - start_time
                self.logger.info(f"PROCESS_END: {filename} status=exception elapsed={elapsed:.2f}s")
        finally:
            if temp_fixed_file and temp_fixed_file.exists():
                try:
                    temp_fixed_file.unlink()
                except Exception as e:
                    self.logger.warning(f"Failed to cleanup temp file {temp_fixed_file}: {e}")
            with self._thread_lock:
                self._active_threads -= 1
                self._thread_lock.notify_all()

    def run(self, input_dirs: Union[Path, List[Path]]):
        input_dirs = self._normalize_input_dirs(input_dirs)
        forced_files: List[Path] = []
        scheduled_input_dirs: Optional[List[Path]] = None
        timed_refresh = False
        next_idle_scan: Dict[Path, float] = {}
        while True:
            # Use updated dirs if changed via Dirs tab
            if self._pending_input_dirs is not None:
                input_dirs = self._pending_input_dirs
                self._pending_input_dirs = None
                next_idle_scan.clear()
            cycle_input_dirs = scheduled_input_dirs or input_dirs
            processed_any = self._run_once(cycle_input_dirs, forced_files=forced_files)
            if timed_refresh and processed_any:
                timed_refresh = False
            now = time.monotonic()
            for input_dir in cycle_input_dirs:
                entry = self._input_dir_entries.get(input_dir)
                if entry is not None and entry.idle_interval is not None:
                    next_idle_scan[input_dir] = now + entry.idle_interval
            forced_files = []
            scheduled_input_dirs = None

            if self._verification_abort_message:
                raise VerificationAbortError(self._verification_abort_message)

            if self._pause_requested:
                from vbc.domain.events import ProcessingPausedOnError
                self.event_bus.publish(
                    ProcessingPausedOnError(message=self._pause_message or "Verification failed")
                )
                self._wait_event.clear()
                self._restart_after_wait = False
                self._wait_event.wait()

                if self._shutdown_requested or self._shutdown_event.is_set():
                    break

                # User pressed R — resume with fresh discovery.
                self._pause_requested = False
                self._pause_message = None
                self._restart_after_wait = False
                with self._refresh_lock:
                    self._refresh_requested = False
                self._wait_event.clear()
                continue

            repaired_paths = self._run_auto_repair(input_dirs)
            if self._shutdown_requested or self._shutdown_event.is_set():
                break
            if repaired_paths:
                forced_files = repaired_paths
                with self._refresh_lock:
                    self._refresh_requested = False
                self._wait_event.clear()
                continue

            if not self.config.general.wait_on_finish:
                break
            if self._shutdown_requested or self._shutdown_event.is_set():
                break

            # Emit bell before entering wait state (if configured)
            if self.config.general.bell_on_finish and not timed_refresh:
                _emit_bell()

            # Publish WAITING state for UI
            from vbc.domain.events import WaitingForInput
            self.event_bus.publish(WaitingForInput())

            # Block until R (restart) or S/Ctrl+C (exit)
            self._wait_event.clear()
            self._restart_after_wait = False
            now = time.monotonic()
            active_paths = set(input_dirs)
            idle_intervals = {
                path: entry.idle_interval
                for path, entry in self._input_dir_entries.items()
                if entry.enabled
                and entry.idle_interval is not None
                and path in active_paths
            }
            for path, interval in idle_intervals.items():
                next_idle_scan.setdefault(path, now + interval)
            next_idle_scan = {
                path: deadline
                for path, deadline in next_idle_scan.items()
                if path in idle_intervals
            }
            timeout = None
            if next_idle_scan:
                timeout = max(0.0, min(next_idle_scan.values()) - now)
            was_signaled = self._wait_event.wait(timeout=timeout)

            if self._shutdown_requested or self._shutdown_event.is_set():
                break

            if not was_signaled and next_idle_scan:
                now = time.monotonic()
                scheduled_input_dirs = [
                    path for path, deadline in next_idle_scan.items() if deadline <= now
                ]
                if not scheduled_input_dirs:
                    scheduled_input_dirs = [
                        min(next_idle_scan, key=next_idle_scan.__getitem__)
                    ]
                timed_refresh = True
                continue

            # User pressed R — reset state and loop for next cycle
            timed_refresh = False
            self._restart_after_wait = False
            self._shutdown_requested = False
            with self._refresh_lock:
                self._refresh_requested = False
            self._wait_event.clear()

    def _run_once(self, input_dirs: List[Path], forced_files: Optional[List[Path]] = None):
        self.logger.info(f"Discovery started: {len(input_dirs)} folders")
        for input_dir in input_dirs:
            self.event_bus.publish(DiscoveryStarted(directory=input_dir))
        files_to_process, discovery_stats = self._perform_discovery(input_dirs)

        forced_video_files: List[VideoFile] = []
        if forced_files:
            existing_paths = {vf.path for vf in files_to_process}
            for forced_path in forced_files:
                if forced_path in existing_paths:
                    continue
                forced_video_file = self._video_file_from_path(forced_path)
                if forced_video_file is None:
                    continue
                forced_video_files.append(forced_video_file)
                existing_paths.add(forced_path)
            if forced_video_files:
                files_to_process = sort_files(
                    files_to_process + forced_video_files,
                    input_dirs,
                    self.config.general,
                    self.file_scanner.extensions,
                )
                discovery_stats["files_found"] += len(forced_video_files)
                discovery_stats["files_to_process"] = len(files_to_process)

        self.logger.info(
            f"Discovery finished: found={discovery_stats['files_found']}, "
            f"to_process={discovery_stats['files_to_process']}, "
            f"already_compressed={discovery_stats['already_compressed']}, "
            f"ignored_err={discovery_stats['ignored_err']}"
        )

        self.event_bus.publish(DiscoveryFinished(
            files_found=discovery_stats['files_found'],
            files_to_process=discovery_stats['files_to_process'],
            already_compressed=discovery_stats['already_compressed'],
            ignored_small=discovery_stats['ignored_small'],
            ignored_err=discovery_stats['ignored_err'],
            ignored_err_entries=discovery_stats['ignored_err_entries'],
            ignored_av1=0,  # AV1 check done during processing
            source_folders_count=len(input_dirs)
        ))

        # If no files to process, exit early
        if len(files_to_process) == 0:
            self.logger.info("No files to process, exiting")
            self.event_bus.publish(ProcessingFinished())
            if self.config.general.bell_on_finish and not self.config.general.wait_on_finish:
                _emit_bell()
            return False

        # Submit-on-demand pattern (like original vbc.py)
        from collections import deque
        pending = deque(files_to_process)
        in_flight = {}  # future -> VideoFile

        # Pre-load metadata for first 25 files (for queue display)
        for vf in list(pending)[:25]:
            if not vf.metadata:
                vf.metadata = self._get_metadata(vf)
        self._prune_failed_pending(pending)

        # Update UI with initial pending files (store VideoFile objects, not just paths)
        self.event_bus.publish(QueueUpdated(pending_files=[vf for vf in pending]))

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            def submit_batch():
                """Submit files up to max_inflight limit"""
                max_inflight = self.config.general.prefetch_factor * self._current_max_threads
                while (
                    len(in_flight) < max_inflight
                    and pending
                    and not self._shutdown_requested
                    and not self._pause_requested
                ):
                    vf = pending.popleft()
                    if vf.path in self._metadata_failed_paths:
                        continue
                    future = executor.submit(self._process_file, vf)
                    in_flight[future] = vf

                # Pre-load metadata for next 25 files in queue (for UI display)
                for vf in list(pending)[:25]:
                    if not vf.metadata:
                        vf.metadata = self._get_metadata(vf)
                self._prune_failed_pending(pending)

                # Update UI with current pending files (store VideoFile objects, not just paths)
                self.event_bus.publish(QueueUpdated(pending_files=[vf for vf in pending]))

            try:
                # Initial batch submission
                submit_batch()

                # Process futures as they complete
                while in_flight:
                    current_futures = set(in_flight.keys())
                    done, _ = concurrent.futures.wait(
                        current_futures,
                        timeout=1.0,
                        return_when=concurrent.futures.FIRST_COMPLETED
                    )

                    for future in done:
                        try:
                            future.result()
                        except Exception as e:
                            import logging
                            logging.error(f"Future failed with exception: {e}")
                        del in_flight[future]

                    # Check for refresh request
                    with self._refresh_lock:
                        if self._refresh_requested:
                            self._refresh_requested = False
                            # Use updated dirs if changed via Dirs tab
                            if self._pending_input_dirs is not None:
                                refresh_dirs = self._pending_input_dirs
                                self._pending_input_dirs = None
                                # Remove stale entries from folder mapping
                                refresh_set = set(refresh_dirs)
                                for old_dir in list(self._folder_mapping.keys()):
                                    if old_dir not in refresh_set:
                                        del self._folder_mapping[old_dir]
                            else:
                                refresh_dirs = list(self._folder_mapping.keys())
                            # Perform new discovery on active folders
                            new_files, new_stats = self._perform_discovery(refresh_dirs)
                            
                            # Identify currently processing files to exclude them from queue
                            in_flight_paths = {vf.identity_path for vf in in_flight.values()}
                            old_pending_paths = {vf.identity_path for vf in pending}
                            
                            # Rebuild pending queue from sorted new_files, excluding in-flight ones
                            # This ensures the queue is fully re-sorted according to config
                            new_pending_list = [
                                vf for vf in new_files if vf.identity_path not in in_flight_paths
                            ]
                            new_pending_paths = {vf.identity_path for vf in new_pending_list}
                            
                            # Calculate stats
                            added = len(new_pending_paths - old_pending_paths)
                            removed = len(old_pending_paths - new_pending_paths)
                            
                            # Replace queue
                            pending = deque(new_pending_list)
                            self._prune_failed_pending(pending)
                            
                            self.event_bus.publish(RefreshFinished(added=added, removed=removed))
                            # Update discovery stats (include ignored_small like old code)
                            self.event_bus.publish(DiscoveryFinished(
                                files_found=new_stats['files_found'],
                                files_to_process=new_stats['files_to_process'],
                                already_compressed=new_stats['already_compressed'],
                                ignored_small=new_stats['ignored_small'],  # FIX: update this counter
                                ignored_err=new_stats['ignored_err'],
                                ignored_err_entries=new_stats['ignored_err_entries'],
                                ignored_av1=0,  # AV1 check done during processing
                                source_folders_count=len(self._folder_mapping)
                            ))
                            # Publish feedback message (like old vbc.py lines 1852-1860)
                            from vbc.domain.events import ActionMessage
                            if added > 0 and removed > 0:
                                self.event_bus.publish(ActionMessage(message=f"Refreshed: +{added} new, -{removed} removed"))
                                self.logger.info(f"Refresh: +{added} new, -{removed} removed")
                            elif added > 0:
                                self.event_bus.publish(ActionMessage(message=f"Refreshed: +{added} new files"))
                                self.logger.info(f"Refresh: added {added} new files to queue")
                            elif removed > 0:
                                self.event_bus.publish(ActionMessage(message=f"Refreshed: -{removed} removed"))
                                self.logger.info(f"Refresh: removed {removed} files from queue")
                            else:
                                self.event_bus.publish(ActionMessage(message="Refreshed: no changes"))
                                self.logger.info("Refresh: no changes detected")

                    # Submit more files to maintain queue
                    submit_batch()

                    # Exit if shutdown requested and no more in flight
                    if (self._shutdown_requested or self._pause_requested) and not in_flight:
                        if self._pause_requested:
                            self.logger.info("Pause requested after verification failure, exiting processing loop")
                        else:
                            self.logger.info("Shutdown requested, exiting processing loop")
                        break

                # After all futures done, give UI one more refresh cycle
                time.sleep(1.5)
                if self._verification_abort_message:
                    raise VerificationAbortError(self._verification_abort_message)
                if not self._shutdown_requested and not self._pause_requested:
                    self.event_bus.publish(ProcessingFinished())
                    if self.config.general.bell_on_finish and not self.config.general.wait_on_finish:
                        _emit_bell()
                self.logger.info("All files processed, exiting")

            except KeyboardInterrupt:
                # User pressed Ctrl+C - graceful shutdown like old vbc.py (lines 1980-1997)
                self.logger.info("Ctrl+C detected - stopping new tasks and interrupting active jobs...")
                from vbc.domain.events import ActionMessage
                self.event_bus.publish(InterruptRequested())
                self.event_bus.publish(ActionMessage(message="Ctrl+C - interrupting active compressions..."))

                # Signal all workers to stop immediately
                self._shutdown_event.set()

                # Stop accepting new tasks
                self._shutdown_requested = True

                # Cancel all pending futures (not yet started)
                for future in list(in_flight.keys()):
                    if not future.done():
                        future.cancel()

                # Wait for currently running tasks to see shutdown_event (max 10 seconds)
                self.logger.info("Waiting for active ffmpeg processes to terminate (max 10s)...")
                deadline = time.monotonic() + 10.0
                while True:
                    running = [future for future in in_flight if not future.done()]
                    if not running:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    concurrent.futures.wait(
                        running,
                        timeout=min(0.2, remaining),
                        return_when=concurrent.futures.FIRST_COMPLETED
                    )

                # Force shutdown after timeout
                executor.shutdown(wait=False, cancel_futures=True)
                self.logger.info("Shutdown complete")

                # Re-raise to propagate to main
                raise

        return True
