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
import threading
import concurrent.futures
import shutil
import logging
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Union, TYPE_CHECKING, Tuple
from vbc.config.models import AppConfig
from vbc.config.rate_control import (
    ResolvedRateControl,
    resolve_rate_control_values,
    format_bps_human,
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
from vbc.domain.models import CompressionJob, JobStatus, VideoFile, VideoMetadata
from vbc.domain.events import DiscoveryStarted, DiscoveryFinished, JobStarted, JobCompleted, JobFailed, QueueUpdated, ProcessingFinished, RefreshFinished
from vbc.ui.keyboard import RequestShutdown, ThreadControlEvent, InterruptRequested
from vbc.pipeline.queue_sorting import sort_files


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
        local_config_registry: Optional["LocalConfigRegistry"] = None,
        cli_overrides: Optional["CliConfigOverrides"] = None,
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

        # Stats
        self.skipped_vbc_count = 0
        self._stats_lock = threading.Lock()

        # Folder mapping (input_dir -> output_dir)
        self._folder_mapping: Dict[Path, Path] = {}
        self._output_dir_map_override: Dict[Path, Path] = output_dir_map or {}
        self._use_output_dir_map_override = output_dir_map is not None

        self._setup_subscriptions()

    def _setup_subscriptions(self):
        from vbc.domain.events import RefreshRequested
        self.event_bus.subscribe(RequestShutdown, self._on_shutdown_request)
        self.event_bus.subscribe(ThreadControlEvent, self._on_thread_control)
        self.event_bus.subscribe(RefreshRequested, self._on_refresh_request)
        self.event_bus.subscribe(InterruptRequested, self._on_interrupt_requested)

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

    def _on_refresh_request(self, event):
        with self._refresh_lock:
            self._refresh_requested = True

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

    def _get_output_dir(self, input_dir: Path) -> Path:
        """Get output directory for given input directory."""
        mapped = self._folder_mapping.get(input_dir)
        if mapped is not None:
            return mapped
        if self._use_output_dir_map_override:
            mapped = self._output_dir_map_override.get(input_dir)
            if mapped is None:
                raise ValueError(f"Output directory mapping missing for {input_dir}")
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
            err_path.parent.mkdir(parents=True, exist_ok=True)
            err_path.write_text(err_msg)
        except Exception:
            return None
        return output_path

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
                if self.config.general.debug and matched_pattern and metadata.custom_cq is not None:
                    raw_model = metadata.camera_raw or "None"
                    self.logger.debug(
                        f"DYNAMIC_CQ_MATCH: {video_file.path.name} "
                        f"pattern=\"{matched_pattern}\" raw=\"{raw_model}\" cq={metadata.custom_cq}"
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
    ) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
        """Return effective rate config (bps/minrate/maxrate) and source label."""
        bps = cfg.general.bps
        minrate = cfg.general.minrate
        maxrate = cfg.general.maxrate
        rate_source = "global"

        if file.metadata and file.metadata.camera_model:
            model = file.metadata.camera_model
            for key, rule in cfg.general.dynamic_quality.items():
                if key in model and rule.rate is not None:
                    bps = rule.rate.bps
                    minrate = rule.rate.minrate
                    maxrate = rule.rate.maxrate
                    rate_source = f"dynamic_quality:{key}"
                    break

        return bps, minrate, maxrate, rate_source

    def _determine_rate_control(self, file: VideoFile, config: Optional[AppConfig] = None) -> ResolvedRateControl:
        cfg = config if config is not None else self.config
        source_bps = None
        if file.metadata and file.metadata.bitrate_kbps and file.metadata.bitrate_kbps > 0:
            source_bps = file.metadata.bitrate_kbps * 1000.0

        bps, minrate, maxrate, rate_source = self._select_rate_config_for_file(file, cfg)

        if cfg.general.debug:
            source_bitrate_text = (
                f"{int(round(source_bps))} bps ({format_bps_human(int(round(source_bps)))})"
                if source_bps is not None
                else "unavailable"
            )
            self.logger.info(
                f"RATE_CONFIG: {file.path.name} "
                f"source_bitrate={source_bitrate_text} "
                f"source={rate_source} bps={bps} minrate={minrate} maxrate={maxrate}"
            )

        resolved = resolve_rate_control_values(
            bps,
            minrate,
            maxrate,
            source_bps,
        )

        if cfg.general.debug:
            resolved_text = (
                f"target={resolved.target_bps} bps ({format_bps_human(resolved.target_bps)}) "
                f"minrate={resolved.minrate_bps if resolved.minrate_bps is not None else 'none'} "
                f"maxrate={resolved.maxrate_bps if resolved.maxrate_bps is not None else 'none'}"
            )
            self.logger.info(f"RATE_RESOLVED: {file.path.name} {resolved_text}")

        return resolved

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

    def _build_vbc_tag_args(
        self,
        source_path: Path,
        quality_label: str,
        encoder: str,
        original_size: int,
        finished_at: str
    ) -> List[str]:
        return [
            f"-XMP:VBCOriginalName={source_path.name}",
            f"-XMP:VBCOriginalSize={original_size}",
            f"-XMP:VBCQuality={quality_label}",
            f"-XMP:VBCEncoder={encoder}",
            f"-XMP:VBCFinishedAt={finished_at}",
        ]

    def _copy_deep_metadata(
        self,
        source_path: Path,
        output_path: Path,
        err_path: Path,
        quality_label: str,
        encoder: str,
        original_size: int,
        finished_at: str
    ) -> None:
        """Copy full metadata from source to output using ExifTool (legacy behavior)."""
        config_path = Path(__file__).resolve().parents[2] / "conf" / "exiftool.conf"
        vbc_tags = self._build_vbc_tag_args(
            source_path,
            quality_label,
            encoder,
            original_size,
            finished_at,
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
                    self.logger.info(
                        f"EXIF_COPY_START: {filename} attempt {attempt}/{max_attempts}"
                    )
                    subprocess.run(
                        exiftool_cmd,
                        capture_output=True,
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
                try:
                    err_path.write_text(
                        f"ExifTool metadata copy timed out after {timeout_s}s (2 attempts)."
                    )
                except Exception:
                    pass
                self.logger.error(
                    f"ExifTool metadata copy timed out after {timeout_s}s (2 attempts) for {filename}"
                )
        else:
            try:
                subprocess.run(exiftool_cmd, capture_output=True, check=True, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self.logger.warning(
                    f"ExifTool metadata copy timed out after {timeout_s}s for {filename}"
                )
            except Exception as e:
                self.logger.warning(f"Failed to copy deep metadata for {filename}: {e}")

    def _write_vbc_tags(
        self,
        source_path: Path,
        output_path: Path,
        quality_label: str,
        encoder: str,
        original_size: int,
        finished_at: str
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
                encoder,
                original_size,
                finished_at,
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
            subprocess.run(exiftool_cmd, capture_output=True, check=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.logger.warning(
                f"ExifTool tag write timed out after {timeout_s}s for {output_path.name}"
            )
        except Exception as e:
            self.logger.warning(f"Failed to write VBC tags for {output_path.name}: {e}")

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
            'ignored_err': 0
        }

        for idx, input_dir in enumerate(input_dirs):
            output_dir = self._folder_mapping.get(input_dir)
            if output_dir is None:
                if self._use_output_dir_map_override:
                    output_dir = self._output_dir_map_override.get(input_dir)
                    if output_dir is None:
                        raise ValueError(f"Output directory mapping missing for {input_dir}")
                else:
                    output_dir = self._resolve_output_dir(input_dir, idx)
                self._folder_mapping[input_dir] = output_dir

            if self.config.general.debug:
                self.logger.info(f"DISCOVERY_START: scanning {input_dir}")

            # Single-pass discovery: collect stats and candidates in one walk
            folder_total_files = 0
            folder_ignored_small = 0
            folder_already_compressed = 0
            folder_ignored_err = 0
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
                            except (OSError, UnicodeDecodeError):
                                folder_ignored_err += 1
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
                    err_path.write_text(err_msg)
                except Exception:
                    pass
                self.logger.error(f"Corrupted file detected (ffprobe failed): {filename} - {e}")
                job = CompressionJob(source_file=video_file, status=JobStatus.FAILED, output_path=output_path, error_message=err_msg)
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
            rate_control: Optional[ResolvedRateControl] = None

            try:
                if job_config.general.quality_mode == "rate":
                    rate_control = self._determine_rate_control(video_file, config=job_config)
                    quality_display = format_bps_human(rate_control.target_bps)
                else:
                    quality_value = self._determine_cq(video_file, use_gpu=use_gpu, config=job_config)
                    quality_display = self._quality_display_for_cq(
                        quality_value,
                        use_gpu=use_gpu,
                        config=job_config,
                    )
            except ValueError as exc:
                err_msg = str(exc)
                failed_job = CompressionJob(
                    source_file=video_file,
                    status=JobStatus.FAILED,
                    output_path=output_path,
                    error_message=err_msg,
                    config_source=config_source,
                )
                with open(err_path, "w") as f:
                    f.write(err_msg)
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
                    rate_control = self._determine_rate_control(video_file, config=job_config)
                    quality_value = None
                    quality_display = format_bps_human(rate_control.target_bps)
                else:
                    quality_value = self._determine_cq(video_file, use_gpu=False, config=job_config)
                    quality_display = self._quality_display_for_cq(
                        quality_value,
                        use_gpu=False,
                        config=job_config,
                    )
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
                if job.output_path.exists():
                    encoder_args = select_encoder_args(job_config, use_gpu)
                    encoder_label = infer_encoder_label(encoder_args, use_gpu)
                    finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    quality_label = job.quality_display or quality_display
                    if job_config.general.copy_metadata:
                        self._copy_deep_metadata(
                            video_file.path,
                            job.output_path,
                            err_path,
                            quality_label,
                            encoder_label,
                            video_file.size_bytes,
                            finished_at
                        )
                    else:
                        self._write_vbc_tags(
                            video_file.path,
                            job.output_path,
                            quality_label,
                            encoder_label,
                            video_file.size_bytes,
                            finished_at
                        )

                    out_size = job.output_path.stat().st_size
                    in_size = video_file.size_bytes
                    ratio = out_size / in_size
                    if ratio > (1.0 - job_config.general.min_compression_ratio):
                        shutil.copy2(video_file.path, job.output_path)
                        job.error_message = f"Ratio {ratio:.2f} above threshold, kept original: {filename}"
                        self.logger.info(
                            f"MIN_RATIO_SKIP: {filename} ratio={ratio:.2f} "
                            f"threshold={job_config.general.min_compression_ratio:.2f} kept_original=True"
                        )

                    if job_config.general.debug and job_config.general.quality_mode == "rate":
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
                with open(err_path, "w") as f:
                    f.write(job.error_message or "Unknown error")
                if self.config.general.debug and start_time:
                    elapsed = time.monotonic() - start_time
                    self.logger.info(f"PROCESS_END: {filename} status={job.status.value} elapsed={elapsed:.2f}s")
            elif job.status == JobStatus.PROCESSING:
                # Status not updated - treat as unknown error
                job.status = JobStatus.FAILED
                job.error_message = "Compression finished but status not updated"
                with open(err_path, "w") as f:
                    f.write(job.error_message)
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
                    with open(err_path, "w") as f:
                        f.write(job.error_message)
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
        self.logger.info(f"Discovery started: {len(input_dirs)} folders")
        for input_dir in input_dirs:
            self.event_bus.publish(DiscoveryStarted(directory=input_dir))
        files_to_process, discovery_stats = self._perform_discovery(input_dirs)

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
            ignored_av1=0,  # AV1 check done during processing
            source_folders_count=len(input_dirs)
        ))

        # If no files to process, exit early
        if len(files_to_process) == 0:
            self.logger.info("No files to process, exiting")
            self.event_bus.publish(ProcessingFinished())
            return

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
                while len(in_flight) < max_inflight and pending and not self._shutdown_requested:
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
                            # Perform new discovery on all folders
                            new_files, new_stats = self._perform_discovery(list(self._folder_mapping.keys()))
                            
                            # Identify currently processing files to exclude them from queue
                            in_flight_paths = {vf.path for vf in in_flight.values()}
                            old_pending_paths = {vf.path for vf in pending}
                            
                            # Rebuild pending queue from sorted new_files, excluding in-flight ones
                            # This ensures the queue is fully re-sorted according to config
                            new_pending_list = [vf for vf in new_files if vf.path not in in_flight_paths]
                            new_pending_paths = {vf.path for vf in new_pending_list}
                            
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
                    if self._shutdown_requested and not in_flight:
                        self.logger.info("Shutdown requested, exiting processing loop")
                        break

                # After all futures done, give UI one more refresh cycle
                time.sleep(1.5)
                if not self._shutdown_requested:
                    self.event_bus.publish(ProcessingFinished())
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
