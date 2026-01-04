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
import threading
import concurrent.futures
import shutil
import logging
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
from vbc.config.models import AppConfig, GeneralConfig
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.file_scanner import FileScanner
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.infrastructure.ffprobe import FFprobeAdapter
from vbc.infrastructure.ffmpeg import FFmpegAdapter
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
    ):
        self.config = config
        self.event_bus = event_bus
        self.file_scanner = file_scanner
        self.exif_adapter = exif_adapter
        self.ffprobe_adapter = ffprobe_adapter
        self.ffmpeg_adapter = ffmpeg_adapter
        self.logger = logging.getLogger(__name__)

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
        output_path = output_dir / rel_path.with_suffix(".mp4")
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
        metadata = VideoMetadata(
            width=width,
            height=height,
            codec=str(stream_info.get("codec", "unknown") or "unknown"),
            audio_codec=stream_info.get("audio_codec"),
            fps=float(stream_info.get("fps") or 0.0),
            megapixels=megapixels,
            color_space=stream_info.get("color_space"),
            duration=float(stream_info.get("duration") or 0.0),
        )

        if self.config.general.use_exif:
            try:
                exif_info = self.exif_adapter.extract_exif_info(video_file, self.config.general.dynamic_cq)
                metadata.camera_model = exif_info.get("camera_model")
                metadata.camera_raw = exif_info.get("camera_raw")
                metadata.custom_cq = exif_info.get("custom_cq")
                metadata.bitrate_kbps = exif_info.get("bitrate_kbps")
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

    def _determine_cq(self, file: VideoFile) -> int:
        """Determines the Constant Quality value based on camera model."""
        default_cq = self.config.general.cq if self.config.general.cq is not None else 45
        if not file.metadata:
            return default_cq
        if file.metadata.custom_cq is not None:
            return file.metadata.custom_cq
        if not file.metadata.camera_model:
            return default_cq
        model = file.metadata.camera_model
        for key, cq_value in self.config.general.dynamic_cq.items():
            if key in model:
                return cq_value
        return default_cq

    def _determine_rotation(self, file: VideoFile) -> Optional[int]:
        """Determines if rotation is needed based on filename pattern."""
        if self.config.general.manual_rotation is not None:
            return self.config.general.manual_rotation
        filename = file.path.name
        for pattern, angle in self.config.autorotate.patterns.items():
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
        cq: int,
        encoder: str,
        original_size: int,
        finished_at: str
    ) -> List[str]:
        return [
            f"-XMP:VBCOriginalName={source_path.name}",
            f"-XMP:VBCOriginalSize={original_size}",
            f"-XMP:VBCQuality={cq}",
            f"-XMP:VBCEncoder={encoder}",
            f"-XMP:VBCFinishedAt={finished_at}",
        ]

    def _copy_deep_metadata(
        self,
        source_path: Path,
        output_path: Path,
        err_path: Path,
        cq: int,
        encoder: str,
        original_size: int,
        finished_at: str
    ) -> None:
        """Copy full metadata from source to output using ExifTool (legacy behavior)."""
        config_path = Path(__file__).resolve().parents[2] / "conf" / "exiftool.conf"
        vbc_tags = self._build_vbc_tag_args(source_path, cq, encoder, original_size, finished_at)

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
        ])
        if config_path.exists():
            exiftool_cmd.extend(vbc_tags)
        exiftool_cmd.extend([
            "-unsafe",
            "-overwrite_original",
            str(output_path)
        ])

        filename = source_path.name
        if self.config.general.debug:
            timeout_s = 30
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
                subprocess.run(exiftool_cmd, capture_output=True, check=True)
            except Exception as e:
                self.logger.warning(f"Failed to copy deep metadata for {filename}: {e}")

    def _write_vbc_tags(
        self,
        source_path: Path,
        output_path: Path,
        cq: int,
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
        exiftool_cmd.extend(self._build_vbc_tag_args(source_path, cq, encoder, original_size, finished_at))
        exiftool_cmd.append(str(output_path))
        try:
            subprocess.run(exiftool_cmd, capture_output=True, check=True)
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

            # Count all files (including small ones) for statistics
            folder_total_files = 0
            folder_ignored_small = 0
            for root, dirs, filenames in os.walk(str(input_dir)):
                root_path = Path(root)
                if root_path.name.endswith("_out"):
                    dirs[:] = []
                    continue
                for fname in filenames:
                    fpath = root_path / fname
                    if fpath.suffix.lower() in self.file_scanner.extensions:
                        folder_total_files += 1
                        try:
                            if fpath.stat().st_size < self.file_scanner.min_size_bytes:
                                folder_ignored_small += 1
                        except OSError:
                            pass

            # Get files that pass size filter
            files = list(self.file_scanner.scan(input_dir))

            # Count files that will be skipped during discovery
            folder_already_compressed = 0
            folder_ignored_err = 0
            folder_files_to_process = []

            for vf in files:
                try:
                    rel_path = vf.path.relative_to(input_dir)
                except ValueError:
                    rel_path = Path(vf.path.name)
                # Always output as .mp4 (lowercase), regardless of input extension
                output_path = output_dir / rel_path.with_suffix('.mp4')
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
                        except:
                            folder_ignored_err += 1
                        if err_path.exists():
                            continue

                # Check if already compressed
                if output_path.exists() and output_path.stat().st_mtime >= vf.path.stat().st_mtime:
                    folder_already_compressed += 1
                    continue

                # AV1 check is done during processing, not discovery
                folder_files_to_process.append(vf)

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

        return all_files, total_stats

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

            # Always output as .mp4 (lowercase), regardless of input extension
            output_path = output_dir / rel_path.with_suffix('.mp4')
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

            target_cq = self._determine_cq(video_file)
            rotation = self._determine_rotation(video_file)

            job_config = self.config.general.model_copy()
            job_config.cq = target_cq

            job = CompressionJob(source_file=video_file, output_path=output_path, rotation_angle=rotation or 0)

            # 2. Compress
            self.event_bus.publish(JobStarted(job=job))
            job.status = JobStatus.PROCESSING
            self.ffmpeg_adapter.compress(job, job_config, rotate=rotation, shutdown_event=self._shutdown_event, input_path=input_path)
            if (
                job.status == JobStatus.HW_CAP_LIMIT
                and self.config.general.cpu_fallback
                and job_config.gpu
            ):
                self.logger.info(f"FFMPEG_FALLBACK: {filename} (hw_cap -> CPU)")
                job_config = job_config.model_copy()
                job_config.gpu = False
                job.status = JobStatus.PROCESSING
                job.error_message = None
                self.ffmpeg_adapter.compress(
                    job,
                    job_config,
                    rotate=rotation,
                    shutdown_event=self._shutdown_event,
                    input_path=input_path,
                )

            # Check final status after compression
            if job.status == JobStatus.COMPLETED:
                if output_path.exists():
                    encoder_label = "NVENC AV1 (GPU)" if job_config.gpu else "SVT-AV1 (CPU)"
                    finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    if self.config.general.copy_metadata:
                        self._copy_deep_metadata(
                            video_file.path,
                            output_path,
                            err_path,
                            job_config.cq,
                            encoder_label,
                            video_file.size_bytes,
                            finished_at
                        )
                    else:
                        self._write_vbc_tags(
                            video_file.path,
                            output_path,
                            job_config.cq,
                            encoder_label,
                            video_file.size_bytes,
                            finished_at
                        )

                    out_size = output_path.stat().st_size
                    in_size = video_file.size_bytes
                    ratio = out_size / in_size
                    if ratio > (1.0 - self.config.general.min_compression_ratio):
                        shutil.copy2(video_file.path, output_path)
                        job.error_message = f"Ratio {ratio:.2f} above threshold, kept original"

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
                            new_paths = {vf.path for vf in new_files}
                            removed = 0
                            if pending:
                                new_pending = deque()
                                for vf in pending:
                                    if vf.path in new_paths:
                                        new_pending.append(vf)
                                    else:
                                        removed += 1
                                self._prune_failed_pending(new_pending)
                                pending = new_pending
                            # Track already submitted files to avoid duplicates
                            submitted_paths = {vf.path for vf in in_flight.values()}
                            submitted_paths.update(vf.path for vf in pending)
                            # Add only new files not already in queue or processing
                            added = 0
                            for vf in new_files:
                                if vf.path not in submitted_paths:
                                    pending.append(vf)
                                    added += 1
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
