"""Domain models for video compression pipeline.

Defines the core entities (VideoFile, CompressionJob) and enumerations that
represent the problem domain, independent of infrastructure and UI.
"""

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class JobStatus(str, Enum):
    """Enumeration of compression job states.

    Attributes:
        PENDING: Waiting to be processed.
        PREFLIGHT: Metadata is being prepared inside an active worker slot.
        PROCESSING: Currently being compressed.
        COMPLETED: Successfully compressed.
        SKIPPED: Skipped due to filter (e.g., already AV1, below min size).
        FAILED: Compression failed; .err marker created.
        HW_CAP_LIMIT: GPU hardware capability exceeded; may retry on CPU if enabled.
        INTERRUPTED: Ctrl+C during processing; partial output may exist.
    """

    PENDING = "PENDING"
    PREFLIGHT = "PREFLIGHT"
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
        camera_model: Inferred camera model from EXIF (for dynamic_quality matching).
        camera_raw: Raw EXIF camera string before normalization.
        custom_cq: Camera-specific CQ override from dynamic_quality[camera].cq.
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


class ManifestProducer(BaseModel):
    """Producer diagnostics carried by a ttracker manifest."""

    model_config = ConfigDict(extra="forbid")

    app: Literal["ttracker"]
    username: str = Field(min_length=1)
    recording_id: str = Field(min_length=1)
    source_size_bytes: int = Field(gt=0)
    source_latest_mtime_ns: int = Field(ge=0)


class ManifestErrorPolicy(BaseModel):
    """Error policy supported by manifest schema version 1."""

    model_config = ConfigDict(extra="forbid")

    missing_input: Literal["fail"]


class CompressionManifest(BaseModel):
    """Strict schema for a manifest-driven concat/transcode request."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    request_id: str = Field(min_length=1)
    created_at: datetime
    producer: ManifestProducer
    operation: Literal["concat_transcode"]
    inputs: List[str] = Field(min_length=1)
    output_path: str = Field(min_length=1)
    source_policy: Literal[
        "keep", "delete_after_success", "move_after_success", "move_all"
    ]
    compression_profile: Literal["tiktok"]
    error_policy: ManifestErrorPolicy

    @field_validator("inputs")
    @classmethod
    def validate_unique_inputs(cls, inputs: List[str]) -> List[str]:
        if len(inputs) != len(set(inputs)):
            raise ValueError("manifest inputs must be unique")
        if any(not value.strip() for value in inputs):
            raise ValueError("manifest inputs cannot contain empty paths")
        return inputs

    @model_validator(mode="after")
    def validate_paths(self):
        input_paths = [Path(value) for value in self.inputs]
        output_path = Path(self.output_path)
        if any(not path.is_absolute() for path in input_paths):
            raise ValueError("manifest inputs must use absolute paths")
        if not output_path.is_absolute():
            raise ValueError("manifest output_path must be absolute")
        if output_path in input_paths:
            raise ValueError("manifest output_path cannot also be an input")
        return self


class MultipartPart(BaseModel):
    """Probed stream facts for one physical manifest input."""

    path: Path
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    codec: str
    audio_codec: Optional[str] = None
    fps: float = Field(ge=0)
    duration: float = Field(ge=0)
    bitrate_kbps: Optional[float] = None
    color_space: Optional[str] = None
    pix_fmt: Optional[str] = None
    video_packets: int = Field(ge=1)
    audio_packets: int = Field(ge=0)
    rebuild_timestamps: bool = False

    @property
    def orientation(self) -> str:
        if self.width == self.height:
            return "square"
        return "portrait" if self.height > self.width else "landscape"


class MetadataRequest(BaseModel):
    """Runtime context for one logical video represented by a JSON manifest."""

    manifest_path: Path
    metadata_dir: Path
    success_dir: Path
    error_dir: Path
    manifest: CompressionManifest
    parts: List[MultipartPart]
    ignored_inputs: List[Path] = Field(default_factory=list)
    source_policy: Literal[
        "keep", "delete_after_success", "move_after_success", "move_all"
    ]
    move_after_success_dir: Optional[Path] = None
    compression_profile: str
    audio_only: Literal["fail", "ignore"]
    target_width: int = Field(gt=0)
    target_height: int = Field(gt=0)

    @property
    def all_input_paths(self) -> List[Path]:
        return [Path(value) for value in self.manifest.inputs]

    @property
    def effective_input_paths(self) -> List[Path]:
        return [part.path for part in self.parts]


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
    metadata_request: Optional[MetadataRequest] = None

    @property
    def identity_path(self) -> Path:
        if self.metadata_request is not None:
            return self.metadata_request.manifest_path
        return self.path

    @property
    def origin_path(self) -> Path:
        return self.identity_path

    @property
    def part_count(self) -> int:
        if self.metadata_request is not None:
            return len(self.metadata_request.manifest.inputs)
        return 1


class CompressionJob(BaseModel):
    """A video compression task being processed or completed.

    Tracks the full lifecycle of a job from discovery through completion,
    including output metadata and error details.

    Attributes:
        source_file: The input video file.
        status: Current job state (PENDING, PROCESSING, COMPLETED, FAILED, etc.).
        output_path: Path where compressed video is written (created during processing).
        output_size_bytes: Final output file size (set when complete).
        output_count: Number of verified output files represented by this job.
        error_message: Error description if status is FAILED or HW_CAP_LIMIT.
        duration_seconds: Wall-clock time spent in FFmpeg (excludes metadata ops).
        rotation_angle: Applied rotation in degrees (0, 90, 180, 270) or None.
        progress_percent: [0-100] progress during encoding (updated by FFmpeg adapter).
        quality_value: CQ/CRF numeric value used for this job (legacy field).
        quality_display: Human-readable quality label (e.g., "CQ45", "200 Mbps").
        config_source: Configuration source (GLOBAL, LOCAL, or CLI).
        verification_passed: Output verification result for completed jobs.
        verification_error: Verification error details when verification fails.
        expected_video_frames: Decoded source frames reported by FFmpeg, adjusted
            for any duplicated or dropped frames.
    """

    source_file: VideoFile
    status: JobStatus = JobStatus.PENDING
    output_path: Optional[Path] = None
    output_size_bytes: Optional[int] = None
    output_count: int = Field(default=1, ge=1)
    error_message: Optional[str] = None
    duration_seconds: Optional[float] = None
    rotation_angle: Optional[int] = None
    progress_percent: float = 0.0
    quality_value: Optional[int] = None
    quality_display: Optional[str] = None
    config_source: ConfigSource = ConfigSource.GLOBAL
    verification_passed: bool = False
    verification_error: Optional[str] = None
    expected_video_frames: Optional[int] = None
