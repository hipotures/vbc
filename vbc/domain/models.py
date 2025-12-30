from enum import Enum
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

class JobStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    HW_CAP_LIMIT = "HW_CAP_LIMIT"
    INTERRUPTED = "INTERRUPTED"  # Ctrl+C during processing

class VideoMetadata(BaseModel):
    width: int
    height: int
    codec: str
    fps: float
    camera_model: Optional[str] = None
    camera_raw: Optional[str] = None
    custom_cq: Optional[int] = None
    bitrate_kbps: Optional[float] = None
    megapixels: Optional[int] = None
    color_space: Optional[str] = None
    duration: Optional[float] = None

class VideoFile(BaseModel):
    path: Path
    size_bytes: int
    metadata: Optional[VideoMetadata] = None

class CompressionJob(BaseModel):
    source_file: VideoFile
    status: JobStatus = JobStatus.PENDING
    output_path: Optional[Path] = None
    output_size_bytes: Optional[int] = None
    error_message: Optional[str] = None
    duration_seconds: Optional[float] = None
    rotation_angle: Optional[int] = None
    progress_percent: float = 0.0
