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
import shlex
from pathlib import Path
from typing import List, Optional
from vbc.domain.models import CompressionJob, JobStatus
from vbc.config.models import AppConfig
from vbc.infrastructure.event_bus import EventBus
from vbc.domain.events import JobProgressUpdated, JobFailed, HardwareCapabilityExceeded

FORMAT_EXTENSION_MAP = {
    "mp4": ".mp4",
    "mov": ".mov",
    "matroska": ".mkv",
    "mkv": ".mkv",
}


def _split_args(args: List[str]) -> List[str]:
    tokens: List[str] = []
    for arg in args:
        tokens.extend(shlex.split(arg))
    return tokens


def _extract_flag_value(args: List[str], flag: str) -> Optional[str]:
    for idx, arg in enumerate(args):
        if arg.strip() == flag and idx + 1 < len(args):
            return str(args[idx + 1]).strip()
        if arg.startswith(f"{flag} "):
            return arg.split(None, 1)[1].strip()
    return None


def extract_quality_value(args: List[str]) -> Optional[int]:
    value = _extract_flag_value(args, "-cq")
    if value is None:
        value = _extract_flag_value(args, "-crf")
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def extract_quality_flag(args: List[str]) -> Optional[str]:
    if _extract_flag_value(args, "-cq") is not None:
        return "-cq"
    if _extract_flag_value(args, "-crf") is not None:
        return "-crf"
    return None


def replace_quality_value(args: List[str], quality: int) -> List[str]:
    updated: List[str] = []
    skip_next = False
    for idx, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        stripped = arg.strip()
        if stripped in ("-cq", "-crf"):
            if idx + 1 < len(args):
                updated.append(stripped)
                updated.append(str(quality))
                skip_next = True
                continue
        if arg.startswith("-cq "):
            updated.append(f"-cq {quality}")
            continue
        if arg.startswith("-crf "):
            updated.append(f"-crf {quality}")
            continue
        updated.append(arg)
    return updated


def extract_output_format(args: List[str]) -> Optional[str]:
    fmt = _extract_flag_value(args, "-f")
    if not fmt:
        return None
    return fmt.strip().lower().lstrip(".")


def output_extension_for_args(args: List[str]) -> str:
    fmt = extract_output_format(args)
    if not fmt:
        return ".mp4"
    return FORMAT_EXTENSION_MAP.get(fmt, ".mp4")


def has_format_arg(args: List[str]) -> bool:
    return extract_output_format(args) is not None


def apply_pix_fmt_arg(args: List[str], pix_fmt: Optional[str]) -> List[str]:
    if not pix_fmt:
        return args
    updated: List[str] = []
    skip_next = False
    replaced = False
    for idx, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg.strip() == "-pix_fmt":
            if idx + 1 < len(args):
                updated.append("-pix_fmt")
                updated.append(pix_fmt)
                skip_next = True
                replaced = True
                continue
        if arg.startswith("-pix_fmt "):
            updated.append(f"-pix_fmt {pix_fmt}")
            replaced = True
            continue
        updated.append(arg)
    if not replaced:
        updated.append(f"-pix_fmt {pix_fmt}")
    return updated


def _update_svt_params(value: str, threads: int) -> str:
    parts = value.split(":") if value else []
    updated = []
    lp_set = False
    for part in parts:
        if part.startswith("lp="):
            updated.append(f"lp={threads}")
            lp_set = True
        else:
            updated.append(part)
    if not lp_set:
        updated.append(f"lp={threads}")
    return ":".join(p for p in updated if p)


def apply_cpu_thread_overrides(args: List[str], threads: Optional[int]) -> List[str]:
    if not threads:
        return args
    updated: List[str] = []
    skip_next = False
    threads_set = False
    for idx, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg.strip() == "-threads":
            if idx + 1 < len(args):
                updated.append("-threads")
                updated.append(str(threads))
                skip_next = True
                threads_set = True
                continue
        if arg.startswith("-threads "):
            updated.append(f"-threads {threads}")
            threads_set = True
            continue
        if arg.strip() == "-svtav1-params":
            if idx + 1 < len(args):
                params = str(args[idx + 1])
                updated.append("-svtav1-params")
                updated.append(_update_svt_params(params, threads))
                skip_next = True
                continue
        if arg.startswith("-svtav1-params "):
            params = arg.split(None, 1)[1]
            updated.append(f"-svtav1-params {_update_svt_params(params, threads)}")
            continue
        updated.append(arg)
    if not threads_set:
        updated.append(f"-threads {threads}")
    return updated


def select_encoder_args(config: AppConfig, use_gpu: bool) -> List[str]:
    if use_gpu:
        encoder_cfg = config.gpu_encoder
    else:
        encoder_cfg = config.cpu_encoder
    args = encoder_cfg.advanced_args if encoder_cfg.advanced else encoder_cfg.common_args
    return list(args)


def infer_encoder_label(args: List[str], use_gpu: bool) -> str:
    if use_gpu:
        return "NVENC AV1 (GPU)"
    codec = _extract_flag_value(args, "-c:v")
    codec = (codec or "").lower()
    if "libaom" in codec:
        return "AOM AV1 (CPU)"
    if "libsvtav1" in codec:
        return "SVT-AV1 (CPU)"
    return "CPU AV1"


def extract_preset(args: List[str]) -> Optional[str]:
    value = _extract_flag_value(args, "-preset")
    return value if value else None

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

    def _build_command(
        self,
        job: CompressionJob,
        config: AppConfig,
        encoder_args: List[str],
        use_gpu: bool,
        rotate: Optional[int] = None,
        input_path: Optional[Path] = None,
    ) -> List[str]:
        """Constructs the FFmpeg command line for AV1 compression.

        Args:
            job: Compression job with source and output paths.
            config: AppConfig with encoder and metadata settings.
            encoder_args: Encoder args selected for this job.
            use_gpu: Whether the GPU encoder is active.
            rotate: Optional rotation angle (90, 180, 270 degrees).
            input_path: Override input (used by color fix recovery).

        Returns:
            Complete FFmpeg command as list of strings.
        """
        cmd = [
            "ffmpeg",
            "-y", # Overwrite output files
        ]
        if use_gpu:
            cmd.extend(["-vsync", "0"])
        cmd.extend([
            "-fflags", "+genpts+igndts",
            "-avoid_negative_ts", "make_zero",
            "-i", str(input_path or job.source_file.path),
        ])

        encoder_tokens = _split_args(encoder_args)
        cmd.extend(encoder_tokens)

        # Audio/Metadata settings
        audio_opts, _, _ = self._select_audio_options(job)
        cmd.extend(audio_opts)
        if config.general.copy_metadata:
            cmd.extend(["-map_metadata", "0"])
            output_fmt = extract_output_format(encoder_args) or "mp4"
            if output_fmt in ("mp4", "mov"):
                cmd.extend(["-movflags", "use_metadata_tags"])
        else:
            cmd.extend(["-map_metadata", "-1"])
        
        # Rotation filter
        if rotate == 180:
            cmd.extend(["-vf", "transpose=2,transpose=2"])
        elif rotate == 90:
            cmd.extend(["-vf", "transpose=1"])
        elif rotate == 270:
            cmd.extend(["-vf", "transpose=2"])

        # Write to .tmp file during compression (renamed on success)
        tmp_path = job.output_path.with_suffix('.tmp')
        if "-f" not in encoder_tokens:
            cmd.extend(["-f", "mp4"])
        cmd.append(str(tmp_path))
        return cmd

    def compress(
        self,
        job: CompressionJob,
        config: AppConfig,
        use_gpu: bool,
        quality: Optional[int] = None,
        rotate: Optional[int] = None,
        shutdown_event=None,
        input_path: Optional[Path] = None,
    ):
        """Execute AV1 compression via FFmpeg subprocess.

        Spawns FFmpeg, monitors stdout for progress updates, detects errors including:
        - Hardware capability exhaustion (HW_CAP_LIMIT status)
        - FFmpeg 7.x color space bugs (triggers _apply_color_fix)
        - Exit code failures

        Publishes JobProgressUpdated, JobFailed, and HardwareCapabilityExceeded events.
        Handles graceful shutdown via shutdown_event (Ctrl+C integration).

        Args:
            job: Compression job to process.
            config: AppConfig with encoder settings and flags.
            use_gpu: Whether the GPU encoder is active.
            quality: Optional quality override (CQ/CRF) for this job.
            rotate: Optional rotation angle (degrees).
            shutdown_event: Threading.Event to signal interruption.
            input_path: Override input path (used for color fix retry).

        Side Effects:
            - Updates job.status, job.error_message, job.duration_seconds
            - Writes .tmp file during processing; renames to output on success
            - Publishes events to EventBus
            - Cleans up .tmp file on error/interruption
        """
        filename = job.source_file.path.name
        start_time = time.monotonic() if config.general.debug else None

        encoder_args = select_encoder_args(config, use_gpu)
        if quality is not None:
            encoder_args = replace_quality_value(encoder_args, quality)
        if not use_gpu:
            encoder_args = apply_cpu_thread_overrides(encoder_args, config.general.ffmpeg_cpu_threads)
            if config.cpu_encoder.advanced and config.cpu_encoder.advanced_enforce_input_pix_fmt:
                pix_fmt = None
                if job.source_file.metadata:
                    pix_fmt = job.source_file.metadata.pix_fmt
                encoder_args = apply_pix_fmt_arg(encoder_args, pix_fmt)

        if config.general.debug:
            quality_value = extract_quality_value(encoder_args)
            quality_flag = extract_quality_flag(encoder_args)
            quality_label = "CQ" if quality_flag == "-cq" else "CRF" if quality_flag == "-crf" else "Q"
            quality_text = f"{quality_label}={quality_value}" if quality_value is not None else "quality=unknown"
            self.logger.info(f"FFMPEG_START: {filename} (gpu={use_gpu}, {quality_text})")

        if config.general.debug:
            _, audio_mode, audio_codec = self._select_audio_options(job)
            self.logger.info(f"AUDIO_MODE: {filename} mode={audio_mode} codec={audio_codec}")

        cmd = self._build_command(job, config, encoder_args, use_gpu, rotate, input_path=input_path)

        if config.general.debug:
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
            if config.general.debug and start_time:
                elapsed = time.monotonic() - start_time
                self.logger.info(f"FFMPEG_END: {filename} status=hw_cap_limit elapsed={elapsed:.2f}s")
        elif color_error:
            # Re-run with color fix remux (recursive call sets final status)
            if config.general.debug:
                self.logger.info(f"FFMPEG_COLORFIX: {filename} (applying color space fix)")
            self._apply_color_fix(job, config, use_gpu, quality, rotate, shutdown_event=shutdown_event)
            # Status is now set by recursive compress() call, don't override
            if config.general.debug and start_time:
                elapsed = time.monotonic() - start_time
                self.logger.info(f"FFMPEG_END: {filename} status={job.status.value} elapsed={elapsed:.2f}s (with colorfix)")
        elif process.returncode != 0:
            job.status = JobStatus.FAILED
            job.error_message = f"ffmpeg exited with code {process.returncode}"
            # Cleanup tmp file on error
            if tmp_path.exists():
                tmp_path.unlink()
            self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
            if config.general.debug and start_time:
                elapsed = time.monotonic() - start_time
                self.logger.info(f"FFMPEG_END: {filename} status=failed code={process.returncode} elapsed={elapsed:.2f}s")
        else:
            # Success - rename .tmp to final output
            if tmp_path.exists():
                tmp_path.rename(job.output_path)
            job.status = JobStatus.COMPLETED
            if config.general.debug and start_time:
                elapsed = time.monotonic() - start_time
                self.logger.info(f"FFMPEG_END: {filename} status=completed elapsed={elapsed:.2f}s")

    def _apply_color_fix(
        self,
        job: CompressionJob,
        config: AppConfig,
        use_gpu: bool,
        quality: Optional[int],
        rotate: Optional[int],
        shutdown_event=None,
    ):
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
                self.compress(
                    job,
                    config,
                    use_gpu,
                    quality=quality,
                    rotate=rotate,
                    shutdown_event=shutdown_event,
                )
            finally:
                # Cleanup and restore
                job.source_file.path = original_path
                if color_fix_path.exists():
                    color_fix_path.unlink()
        else:
            job.status = JobStatus.FAILED
            job.error_message = "Color fix remux failed"
            self.event_bus.publish(JobFailed(job=job, error_message=job.error_message))
