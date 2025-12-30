from typing import List, Dict, Optional
from pydantic import BaseModel, Field, field_validator

class GpuConfig(BaseModel):
    """GPU monitoring and sparkline configuration."""
    enabled: bool = True
    refresh_rate: int = Field(default=5, ge=1)
    sample_interval_s: float = Field(default=5.0, ge=0.1)
    history_window_s: float = Field(default=300.0, ge=10.0)  # 5 min
    nvtop_device_index: int = Field(default=0, ge=0)
    nvtop_device_name: Optional[str] = None  # Override index

class GeneralConfig(BaseModel):
    threads: int = Field(default=1, gt=0)
    cq: Optional[int] = Field(default=45, ge=0, le=63)
    prefetch_factor: int = Field(default=1, ge=1)
    gpu: bool = True
    gpu_refresh_rate: int = Field(default=5, ge=1)
    copy_metadata: bool = True
    use_exif: bool = True
    filter_cameras: List[str] = Field(default_factory=list)
    dynamic_cq: Dict[str, int] = Field(default_factory=dict)
    extensions: List[str] = Field(default_factory=lambda: [".mp4", ".mov", ".avi", ".flv", ".webm"])
    min_size_bytes: int = Field(default=1048576)
    clean_errors: bool = False
    skip_av1: bool = False
    strip_unicode_display: bool = True
    manual_rotation: Optional[int] = Field(default=None)
    min_compression_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    debug: bool = False

class AutoRotateConfig(BaseModel):
    patterns: Dict[str, int] = Field(default_factory=dict)

    @field_validator('patterns')
    @classmethod
    def validate_angles(cls, v: Dict[str, int]) -> Dict[str, int]:
        for pattern, angle in v.items():
            if angle not in {0, 90, 180, 270}:
                raise ValueError(f"Invalid rotation angle {angle} for pattern {pattern}. Must be 0, 90, 180, or 270.")
        return v

class UiConfig(BaseModel):
    """UI display configuration."""
    activity_feed_max_items: int = Field(default=5, ge=1, le=20)
    active_jobs_max_display: int = Field(default=8, ge=1, le=16)  # Max threads to reserve space for
    panel_height_scale: float = Field(default=0.7, ge=0.3, le=1.0)  # 0.7 = 30% reduction

class AppConfig(BaseModel):
    general: GeneralConfig
    autorotate: AutoRotateConfig = Field(default_factory=AutoRotateConfig)
    gpu_config: GpuConfig = Field(default_factory=GpuConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
