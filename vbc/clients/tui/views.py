"""Display objects for the copied Rich renderers, built from API DTOs.

The renderers (dashboard, overlays) were written against domain objects
(``job.source_file.path.name``, ``job.source_file.metadata.fps``,
``job.status == JobStatus.PREFLIGHT``). These views provide exactly that
shape from ``vbc.contracts`` without importing server code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from vbc import contracts as dto


class JobStatus(str, Enum):
    """Mirror of the server's job states (values are the wire strings)."""

    PENDING = "PENDING"
    PREFLIGHT = "PREFLIGHT"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    HW_CAP_LIMIT = "HW_CAP_LIMIT"
    INTERRUPTED = "INTERRUPTED"

    @classmethod
    def parse(cls, value: str) -> "JobStatus":
        try:
            return cls(value)
        except ValueError:
            return cls.PROCESSING


class ConfigSourceView(str, Enum):
    GLOBAL = "GLOBAL"
    LOCAL = "LOCAL"
    CLI = "CLI"

    @classmethod
    def parse(cls, value: Optional[str]) -> "ConfigSourceView":
        """The API sends the server's letters (G/L/C); full names are accepted too."""
        text = (value or "").strip().upper()
        for member in cls:
            if text in (member.value, member.value[0]):
                return member
        return cls.GLOBAL


@dataclass
class MediaView:
    width: int = 0
    height: int = 0
    fps: Optional[float] = None
    duration: Optional[float] = None
    codec: Optional[str] = None
    audio_codec: Optional[str] = None
    camera_model: Optional[str] = None
    custom_cq: Optional[int] = None

    @classmethod
    def from_dto(cls, media: Optional[dto.MediaInfo]) -> Optional["MediaView"]:
        if media is None:
            return None
        return cls(
            width=media.width or 0, height=media.height or 0, fps=media.fps, duration=media.duration_s,
            codec=media.codec, audio_codec=media.audio_codec, camera_model=media.camera_model,
            custom_cq=media.custom_cq,
        )


@dataclass
class FileView:
    path: Path
    size_bytes: int
    metadata: Optional[MediaView] = None
    part_count: int = 1

    @property
    def identity_path(self) -> Path:
        return self.path

    @property
    def origin_path(self) -> Path:
        return self.path


@dataclass
class JobView:
    id: str
    source_file: FileView
    status: JobStatus
    quality_display: Optional[str] = None
    quality_value: Optional[int] = None
    rotation_angle: Optional[int] = None
    config_source: ConfigSourceView = ConfigSourceView.GLOBAL
    error_message: Optional[str] = None
    output_size_bytes: Optional[int] = None
    duration_seconds: Optional[float] = None
    progress_percent: float = 0.0
    warning: Optional[str] = None

    @classmethod
    def from_active(cls, job: dto.ActiveJob) -> "JobView":
        return cls(
            id=job.id,
            source_file=FileView(Path(job.path), job.input_bytes, MediaView.from_dto(job.media), job.part_count),
            status=JobStatus.parse(job.status),
            quality_display=job.quality, quality_value=job.quality_value,
            rotation_angle=job.rotation_angle, config_source=ConfigSourceView.parse(job.config_source),
        )

    @classmethod
    def from_recent(cls, job: dto.RecentJob) -> "JobView":
        return cls(
            id=job.id,
            source_file=FileView(Path(job.path), job.input_bytes, MediaView.from_dto(job.media)),
            status=JobStatus.parse(job.status),
            quality_display=job.quality, quality_value=job.quality_value,
            rotation_angle=job.rotation_angle, config_source=ConfigSourceView.parse(job.config_source),
            error_message=job.error_message, output_size_bytes=job.output_bytes,
            duration_seconds=job.duration_s, warning=job.warning,
        )


@dataclass(frozen=True)
class SessionErrorEntry:
    """Same fields as legacy ui.state.SessionErrorEntry, from /errors."""

    path: Path
    size_bytes: Optional[int]
    width: Optional[int]
    height: Optional[int]
    fps: Optional[float]
    codec: Optional[str]
    audio_codec: Optional[str]
    duration_seconds: Optional[float]
    error_message: str
    created_at: datetime

    @classmethod
    def from_dto(cls, entry: dto.ErrorEntry, offset_ms: int) -> "SessionErrorEntry":
        return cls(
            path=Path(entry.path), size_bytes=entry.size_bytes, width=entry.width, height=entry.height,
            fps=entry.fps, codec=entry.codec, audio_codec=entry.audio_codec,
            duration_seconds=entry.duration_s, error_message=entry.error_message,
            created_at=datetime.fromtimestamp((entry.created_at - offset_ms) / 1000),
        )


def local_datetime(server_ms: Optional[int], offset_ms: int) -> Optional[datetime]:
    """Server epoch ms -> local naive datetime (as the renderers expect)."""
    if server_ms is None:
        return None
    return datetime.fromtimestamp((server_ms - offset_ms) / 1000)
