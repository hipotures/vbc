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
    """Wrapper around ffmpeg for video compression."""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.logger = logging.getLogger(__name__)

    def _build_command(self, job: CompressionJob, config: GeneralConfig, rotate: Optional[int] = None, input_path: Optional[Path] = None) -> List[str]:
        """Constructs the ffmpeg command line arguments."""
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
        cmd.extend([
            "-c:a", "copy",
        ])
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
        """Executes the compression process."""
        filename = job.source_file.path.name
        start_time = time.monotonic() if config.debug else None

        if config.debug:
            self.logger.info(f"FFMPEG_START: {filename} (gpu={config.gpu}, cq={config.cq})")

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

                if "Hardware is lacking required capabilities" in line:
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
        """Special handling for FFmpeg 7.x 'reserved' color space bug."""
        # 1. Create a remuxed file with metadata filters
        color_fix_path = job.output_path.with_name(f"{job.output_path.stem}_colorfix.mp4")

        # Check if source is HEVC or H264 to apply correct bitstream filter
        # For simplicity we try to apply hevc_metadata then fallback
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
