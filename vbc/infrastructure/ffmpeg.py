"""FFmpeg process wrapper for AV1 video compression.

Handles subprocess lifecycle, progress monitoring, error detection, and recovery.
Detects hardware capability errors and color space issues, publishing events on failures.
"""

import subprocess
import re
import logging
import time
import threading
import queue
from pathlib import Path
from typing import List, Optional
from vbc.domain.models import CompressionJob, JobStatus
from vbc.config.models import GeneralConfig
from vbc.infrastructure.event_bus import EventBus
from vbc.domain.events import JobProgressUpdated, JobFailed, HardwareCapabilityExceeded


class FFmpegAdapter:
    """Subprocess adapter for FFmpeg video compression.

    Manages FFmpeg execution with real-time progress monitoring via stdout parsing.
    Detects GPU hardware capability exhaustion (exit code 187) and color space bugs
    in FFmpeg 7.x, triggering automatic recovery via remuxing + retry.
    """

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.logger = logging.getLogger(__name__)

    def _select_audio_options(self, job: CompressionJob) -> tuple[List[str], str, str]:
        raw = ""
        if job.source_file.metadata and job.source_file.metadata.audio_codec:
            raw = str(job.source_file.metadata.audio_codec).lower()

        # Normalize to ffprobe-like codec_name (strip profile/extra text)
        audio_codec = re.split(r"[,\s(]", raw, maxsplit=1)[0] if raw else ""

        lossless_codecs = {"flac", "alac", "truehd", "mlp", "wavpack", "ape", "tta"}

        # If no audio codec info, treat as unknown and re-encode (safer for MP4)
        if not audio_codec:
            return (["-c:a", "aac", "-b:a", "192k"], "aac 192k", "unknown")

        if audio_codec.startswith("pcm_") or audio_codec in lossless_codecs:
            return (["-c:a", "aac", "-b:a", "256k"], "aac 256k", audio_codec)

        # Safe copies to MP4 (minimal set)
        if audio_codec in {"aac", "mp3"}:
            return (["-c:a", "copy"], "copy", audio_codec)

        # Everything else: re-encode to AAC for container compatibility
        return (["-c:a", "aac", "-b:a", "192k"], "aac 192k", audio_codec)

    def _build_command(self, job: CompressionJob, config: GeneralConfig, rotate: Optional[int] = None, input_path: Optional[Path] = None) -> List[str]:
        """Constructs the FFmpeg command line for AV1 compression.

        Args:
            job: Compression job with source and output paths.
            config: Config with GPU/CPU choice, CQ, thread counts.
            rotate: Optional rotation angle (90, 180, 270 degrees).
            input_path: Override input (used by color fix recovery).

        Returns:
            Complete FFmpeg command as list of strings.
        """
        cmd = [
            "ffmpeg",
            "-y", # Overwrite output files
        ]
        if config.gpu:
            cmd.extend(["-vsync", "0"])
        cmd.extend([
            "-fflags", "+genpts+igndts",
            "-avoid_negative_ts", "make_zero",
            "-i", str(input_path or job.source_file.path),
        ])
        
        # Video encoding settings
        if config.gpu:
            cmd.extend([
                "-c:v", "av1_nvenc",
                "-cq", str(config.cq),
                "-preset", "p7",
                "-tune", "hq",
                "-b:v", "0"
            ])
        else:
            svt_params = "tune=0:enable-overlays=1"
            if config.ffmpeg_cpu_threads:
                svt_params = f"{svt_params}:lp={config.ffmpeg_cpu_threads}"
            cmd.extend([
                "-c:v", "libsvtav1",
                "-preset", "6",
                "-crf", str(config.cq),
                "-svtav1-params", svt_params
            ])
            if config.ffmpeg_cpu_threads:
                cmd.extend(["-threads", str(config.ffmpeg_cpu_threads)])
            
        # Audio/Metadata settings
        audio_opts, _, _ = self._select_audio_options(job)
        cmd.extend(audio_opts)
        if config.copy_metadata:
            cmd.extend(["-map_metadata", "0", "-movflags", "use_metadata_tags"])
        else:
            cmd.extend(["-map_metadata", "-1"])
        
        # Rotation filter
        if rotate == 180:
            cmd.extend(["-vf", "transpose=2,transpose=2"])
        elif rotate == 90:
            cmd.extend(["-vf", "transpose=1"])
        elif rotate == 270:
            cmd.extend(["-vf", "transpose=2"])

        # Write to .tmp file during compression (renamed to .mp4 on success)
        # Force mp4 format since .tmp extension doesn't indicate format
        tmp_path = job.output_path.with_suffix('.tmp')
        cmd.extend(["-f", "mp4", str(tmp_path)])
        return cmd

    def compress(self, job: CompressionJob, config: GeneralConfig, rotate: Optional[int] = None, shutdown_event=None, input_path: Optional[Path] = None):
        """Execute AV1 compression via FFmpeg subprocess.

        Spawns FFmpeg, monitors stdout for progress updates, detects errors including:
        - Hardware capability exhaustion (HW_CAP_LIMIT status)
        - FFmpeg 7.x color space bugs (triggers _apply_color_fix)
        - Exit code failures

        Publishes JobProgressUpdated, JobFailed, and HardwareCapabilityExceeded events.
        Handles graceful shutdown via shutdown_event (Ctrl+C integration).

        Args:
            job: Compression job to process.
            config: Config with GPU/CPU, CQ, debug flag.
            rotate: Optional rotation angle (degrees).
            shutdown_event: Threading.Event to signal interruption.
            input_path: Override input path (used for color fix retry).

        Side Effects:
            - Updates job.status, job.error_message, job.duration_seconds
            - Writes .tmp file during processing; renames to .mp4 on success
            - Publishes events to EventBus
            - Cleans up .tmp file on error/interruption
        """
        filename = job.source_file.path.name
        start_time = time.monotonic() if config.debug else None

        if config.debug:
            self.logger.info(f"FFMPEG_START: {filename} (gpu={config.gpu}, cq={config.cq})")

        if config.debug:
            _, audio_mode, audio_codec = self._select_audio_options(job)
            self.logger.info(f"AUDIO_MODE: {filename} mode={audio_mode} codec={audio_codec}")

        cmd = self._build_command(job, config, rotate, input_path=input_path)

        if config.debug:
            self.logger.debug(f"FFMPEG_CMD: {' '.join(cmd)}")

        # Use duration for progress calculation
        total_duration = job.source_file.metadata.duration if job.source_file.metadata else 0.0

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        # Regex to parse 'time=00:00:00.00' from ffmpeg output
        time_regex = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
        hw_cap_error = False
        color_error = False

        output_queue: "queue.Queue[Optional[str]]" = queue.Queue()

        def _reader():
            if not process.stdout:
                output_queue.put(None)
                return
            for line in process.stdout:
                output_queue.put(line)
            output_queue.put(None)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        try:
            while True:
                # Check for shutdown signal from orchestrator
                if shutdown_event and shutdown_event.is_set():
                    self.logger.info(f"FFMPEG_INTERRUPTED: {filename} (shutdown signal)")
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

                    # Clean up tmp file
                    tmp_path = job.output_path.with_suffix('.tmp')
                    if tmp_path.exists():
                        tmp_path.unlink()

                    # Set INTERRUPTED status and return (don't raise exception)
                    job.status = JobStatus.INTERRUPTED
                    job.error_message = "Interrupted by user (Ctrl+C)"
                    return  # Exit compress() early

                try:
                    line = output_queue.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue

                if line is None:
                    break

                if (
                    "Hardware is lacking required capabilities" in line
                    or "No capable devices found" in line
                    or "not supported" in line and "nvenc" in line.lower()
                ):
                    hw_cap_error = True
                if "is not a valid value for color_primaries" in line or "is not a valid value for color_trc" in line:
                    color_error = True

                match = time_regex.search(line)
                if match:
                    h, m, s = map(float, match.groups())
                    current_seconds = h * 3600 + m * 60 + s
                    if total_duration > 0:
                        progress_percent = min(100.0, (current_seconds / total_duration) * 100.0)
                        self.event_bus.publish(JobProgressUpdated(job=job, progress_percent=progress_percent))

            process.wait()
        except KeyboardInterrupt:
            # User pressed Ctrl+C directly in this thread (shouldn't happen with daemon threads)
            self.logger.info(f"FFMPEG_INTERRUPTED: {filename} (KeyboardInterrupt)")
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

            # Clean up tmp file
            tmp_path = job.output_path.with_suffix('.tmp')
            if tmp_path.exists():
                tmp_path.unlink()

            # Set status and re-raise to propagate to orchestrator
            job.status = JobStatus.INTERRUPTED
            job.error_message = "Interrupted by user (Ctrl+C)"
            raise

        # Get tmp file path
        tmp_path = job.output_path.with_suffix('.tmp')

        # Check for hardware capability error (code 187 or text match)
        if hw_cap_error or process.returncode == 187:
            job.status = JobStatus.HW_CAP_LIMIT
            job.error_message = "Hardware is lacking required capabilities"
            # Cleanup tmp file on error
            if tmp_path.exists():
                tmp_path.unlink()
            self.event_bus.publish(HardwareCapabilityExceeded(job=job))
            if config.debug and start_time:
                elapsed = time.monotonic() - start_time
                self.logger.info(f"FFMPEG_END: {filename} status=hw_cap_limit elapsed={elapsed:.2f}s")
        elif color_error:
            # Re-run with color fix remux (recursive call sets final status)
            if config.debug:
                self.logger.info(f"FFMPEG_COLORFIX: {filename} (applying color space fix)")
            self._apply_color_fix(job, config, rotate, shutdown_event=shutdown_event)
            # Status is now set by recursive compress() call, don't override
            if config.debug and start_time:
                elapsed = time.monotonic() - start_time
                self.logger.info(f"FFMPEG_END: {filename} status={job.status.value} elapsed={elapsed:.2f}s (with colorfix)")
        elif process.returncode != 0:
            job.status = JobStatus.FAILED
            job.error_message = f"ffmpeg exited with code {process.returncode}"
            # Cleanup tmp file on error
            if tmp_path.exists():
                tmp_path.unlink()
            self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
            if config.debug and start_time:
                elapsed = time.monotonic() - start_time
                self.logger.info(f"FFMPEG_END: {filename} status=failed code={process.returncode} elapsed={elapsed:.2f}s")
        else:
            # Success - rename .tmp to final .mp4
            if tmp_path.exists():
                tmp_path.rename(job.output_path)
            job.status = JobStatus.COMPLETED
            if config.debug and start_time:
                elapsed = time.monotonic() - start_time
                self.logger.info(f"FFMPEG_END: {filename} status=completed elapsed={elapsed:.2f}s")

    def _apply_color_fix(self, job: CompressionJob, config: GeneralConfig, rotate: Optional[int], shutdown_event=None):
        """Recovery for FFmpeg 7.x color space metadata bug.

        FFmpeg 7.x rejects "reserved" color_primaries/color_trc/colorspace values.
        Solution: remux input with explicit color metadata values, then re-compress.

        1. Create intermediate .mp4 with bitstream filter (hevc_metadata or h264_metadata)
        2. Run compress() using remuxed file (recursive call)
        3. Clean up intermediate file

        This is a workaround for upstream FFmpeg issue; remove if FFmpeg < 7.x no longer used.

        Args:
            job: Compression job (source_file.path modified and restored).
            config: Compression configuration.
            rotate: Rotation angle.
            shutdown_event: Shutdown signal from orchestrator.

        Side Effects:
            - Modifies job.source_file.path temporarily
            - Creates and deletes _colorfix.mp4 intermediate file
            - Sets job.status and job.error_message on remux failure
        """
        color_fix_path = job.output_path.with_name(f"{job.output_path.stem}_colorfix.mp4")

        # Try HEVC metadata filter first, fall back to H.264 if needed
        remux_cmd = [
            "ffmpeg", "-y", "-i", str(job.source_file.path),
            "-c", "copy",
            "-bsf:v", "hevc_metadata=color_primaries=1:color_trc=1:colorspace=1",
            str(color_fix_path)
        ]

        res = subprocess.run(remux_cmd, capture_output=True)
        if res.returncode != 0:
            # Try H264 variant
            remux_cmd[5] = "h264_metadata=color_primaries=1:color_trc=1:colorspace=1"
            res = subprocess.run(remux_cmd, capture_output=True)

        if res.returncode == 0:
            # 2. Run compression using the colorfix file as input
            original_path = job.source_file.path
            job.source_file.path = color_fix_path
            try:
                self.compress(job, config, rotate, shutdown_event=shutdown_event)
            finally:
                # Cleanup and restore
                job.source_file.path = original_path
                if color_fix_path.exists():
                    color_fix_path.unlink()
        else:
            job.status = JobStatus.FAILED
            job.error_message = "Color fix remux failed"
            self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
