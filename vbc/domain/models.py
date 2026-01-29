"""Domain models for video compression pipeline.

Defines the core entities (VideoFile, CompressionJob) and enumerations that
represent the problem domain, independent of infrastructure and UI.
"""

from enum import Enum
from pathlib import Path
from typing import Optional
from pydantic import BaseModel


class JobStatus(str, Enum):
    """Enumeration of compression job states.

    Attributes:
        PENDING: Waiting to be processed.
        PROCESSING: Currently being compressed.
        COMPLETED: Successfully compressed.
        SKIPPED: Skipped due to filter (e.g., already AV1, below min size).
        FAILED: Compression failed; .err marker created.
        HW_CAP_LIMIT: GPU hardware capability exceeded; may retry on CPU if enabled.
        INTERRUPTED: Ctrl+C during processing; partial output may exist.
    """

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    HW_CAP_LIMIT = "HW_CAP_LIMIT"
    INTERRUPTED = "INTERRUPTED"


class ConfigSource(str, Enum):
    """Source of configuration parameters.

    Attributes:
        GLOBAL: Global conf/vbc.yaml configuration.
        LOCAL: Local VBC.YAML in video directory.
        CLI: CLI arguments (highest priority).
    """

    GLOBAL = "G"
    LOCAL = "L"
    CLI = "C"

class VideoMetadata(BaseModel):
    """Extracted video stream information.

    Attributes:
        width, height: Dimensions in pixels.
        codec: Primary video codec name (h264, hevc, av1, etc.).
        audio_codec: Primary audio codec name (pcm_s16le, aac, etc.).
        fps: Frames per second.
        camera_model: Inferred camera model from EXIF (for dynamic_cq matching).
        camera_raw: Raw EXIF camera string before normalization.
        custom_cq: Camera-specific CQ override from dynamic_cq config.
        bitrate_kbps: Input stream bitrate.
        megapixels: Estimated megapixel value (for reference).
        color_space: FFmpeg color space (e.g., "bt709", "yuv420p").
        pix_fmt: Pixel format from ffprobe (e.g., "yuv420p10le").
        duration: Total duration in seconds.
    """

    width: int
    height: int
    codec: str
    audio_codec: Optional[str] = None
    fps: float
    camera_model: Optional[str] = None
    camera_raw: Optional[str] = None
    custom_cq: Optional[int] = None
    bitrate_kbps: Optional[float] = None
    megapixels: Optional[int] = None
    color_space: Optional[str] = None
    pix_fmt: Optional[str] = None
    duration: Optional[float] = None
    vbc_encoded: bool = False

class VideoFile(BaseModel):
    """A discovered video file to process.

    Attributes:
        path: Full path to the source file.
        size_bytes: File size in bytes.
        metadata: Video stream information (extracted during discovery or processing).
    """

    path: Path
    size_bytes: int
    metadata: Optional[VideoMetadata] = None

class CompressionJob(BaseModel):
    """A video compression task being processed or completed.

    Tracks the full lifecycle of a job from discovery through completion,
    including output metadata and error details.

    Attributes:
        source_file: The input video file.
        status: Current job state (PENDING, PROCESSING, COMPLETED, FAILED, etc.).
        output_path: Path where compressed video is written (created during processing).
        output_size_bytes: Final output file size (set when complete).
        error_message: Error description if status is FAILED or HW_CAP_LIMIT.
        duration_seconds: Wall-clock time spent in FFmpeg (excludes metadata ops).
        rotation_angle: Applied rotation in degrees (0, 90, 180, 270) or None.
        progress_percent: [0-100] progress during encoding (updated by FFmpeg adapter).
        quality_value: CQ quality value used for this job.
        config_source: Configuration source (GLOBAL, LOCAL, or CLI).
    """

    source_file: VideoFile
    status: JobStatus = JobStatus.PENDING
    output_path: Optional[Path] = None
    output_size_bytes: Optional[int] = None
    error_message: Optional[str] = None
    duration_seconds: Optional[float] = None
    rotation_angle: Optional[int] = None
    progress_percent: float = 0.0
    quality_value: Optional[int] = None
    config_source: ConfigSource = ConfigSource.GLOBAL
