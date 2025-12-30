from typing import List, Dict, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

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
    input_dirs: List[str] = Field(default_factory=list)
    autorotate: AutoRotateConfig = Field(default_factory=AutoRotateConfig)
    gpu_config: GpuConfig = Field(default_factory=GpuConfig)
    ui: UiConfig = Field(default_factory=UiConfig)

class DemoExtension(BaseModel):
    ext: str
    weight: float = Field(default=1.0, gt=0)

class DemoFilesConfig(BaseModel):
    count: int = Field(default=120, ge=0)
    extensions: List[DemoExtension] = Field(default_factory=list)
    min_words: int = Field(default=1, ge=1)
    max_words: int = Field(default=3, ge=1)
    separator: str = Field(default="-")

    @model_validator(mode="after")
    def validate_word_range(self):
        if self.max_words < self.min_words:
            raise ValueError("max_words must be >= min_words")
        return self

class DemoSizeConfig(BaseModel):
    distribution: str = Field(default="triangular")
    min_mb: float = Field(default=20.0, gt=0)
    mode_mb: float = Field(default=180.0, gt=0)
    max_mb: float = Field(default=1800.0, gt=0)

    @field_validator("distribution")
    @classmethod
    def validate_distribution(cls, v: str) -> str:
        allowed = {"triangular", "uniform"}
        if v not in allowed:
            raise ValueError(f"Unsupported distribution: {v}. Use one of {sorted(allowed)}")
        return v

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.min_mb > self.max_mb:
            raise ValueError("min_mb must be <= max_mb")
        if self.distribution == "triangular" and not (self.min_mb <= self.mode_mb <= self.max_mb):
            raise ValueError("mode_mb must be between min_mb and max_mb")
        return self

class DemoBitrateConfig(BaseModel):
    min_mbps: float = Field(default=10.0, gt=0)
    mode_mbps: float = Field(default=35.0, gt=0)
    max_mbps: float = Field(default=120.0, gt=0)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.min_mbps > self.max_mbps:
            raise ValueError("min_mbps must be <= max_mbps")
        if not (self.min_mbps <= self.mode_mbps <= self.max_mbps):
            raise ValueError("mode_mbps must be between min_mbps and max_mbps")
        return self

class DemoResolution(BaseModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    weight: float = Field(default=1.0, gt=0)

class DemoFps(BaseModel):
    value: float = Field(gt=0)
    weight: float = Field(default=1.0, gt=0)

class DemoCodec(BaseModel):
    name: str
    weight: float = Field(default=1.0, gt=0)

class DemoProcessingConfig(BaseModel):
    throughput_mb_s: float = Field(default=35.0, gt=0)
    progress_interval_s: float = Field(default=0.2, gt=0)
    jitter_pct: float = Field(default=0.25, ge=0.0, le=1.0)

class DemoOutputRatioConfig(BaseModel):
    min: float = Field(default=0.22, ge=0.0, le=1.0)
    max: float = Field(default=0.55, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.min > self.max:
            raise ValueError("output_ratio.min must be <= output_ratio.max")
        return self

class DemoErrorType(BaseModel):
    type: str
    weight: float = Field(default=1.0, gt=0)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        allowed = {"ffprobe_failed", "ffmpeg_error", "hw_cap", "av1_skip", "camera_skip"}
        if v not in allowed:
            raise ValueError(f"Unsupported error type: {v}. Use one of {sorted(allowed)}")
        return v

class DemoErrorsConfig(BaseModel):
    total: int = Field(default=6, ge=0)
    types: List[DemoErrorType] = Field(default_factory=list)

class DemoKeptOriginalConfig(BaseModel):
    count: int = Field(default=3, ge=0)

class DemoDiscoveryConfig(BaseModel):
    already_compressed: int = Field(default=8, ge=0)
    ignored_small: int = Field(default=4, ge=0)
    ignored_err: int = Field(default=2, ge=0)

def _demo_default_extensions() -> List[DemoExtension]:
    return [
        DemoExtension(ext=".mp4", weight=0.55),
        DemoExtension(ext=".mov", weight=0.25),
        DemoExtension(ext=".mkv", weight=0.20),
    ]

def _demo_default_resolutions() -> List[DemoResolution]:
    return [
        DemoResolution(width=3840, height=2160, weight=0.30),
        DemoResolution(width=1920, height=1080, weight=0.55),
        DemoResolution(width=1280, height=720, weight=0.15),
    ]

def _demo_default_fps() -> List[DemoFps]:
    return [
        DemoFps(value=23.976, weight=0.20),
        DemoFps(value=25.0, weight=0.20),
        DemoFps(value=29.97, weight=0.40),
        DemoFps(value=59.94, weight=0.20),
    ]

def _demo_default_codecs() -> List[DemoCodec]:
    return [
        DemoCodec(name="h264", weight=0.45),
        DemoCodec(name="hevc", weight=0.35),
        DemoCodec(name="av1", weight=0.20),
    ]

def _demo_default_camera_models() -> List[str]:
    return [
        "Sony FX3",
        "Sony A7S III",
        "Panasonic GH6",
        "Panasonic S5II",
        "DJI Pocket 3",
        "Canon R5",
        "GoPro HERO11",
    ]

def _demo_default_error_types() -> List[DemoErrorType]:
    return [
        DemoErrorType(type="ffprobe_failed", weight=0.20),
        DemoErrorType(type="ffmpeg_error", weight=0.50),
        DemoErrorType(type="hw_cap", weight=0.30),
    ]

class DemoConfig(BaseModel):
    seed: Optional[int] = None
    input_folders: List[str] = Field(default_factory=lambda: ["DEMO/Studio_A", "DEMO/Studio_B"])
    files: DemoFilesConfig = Field(default_factory=DemoFilesConfig)
    sizes: DemoSizeConfig = Field(default_factory=DemoSizeConfig)
    bitrate_mbps: DemoBitrateConfig = Field(default_factory=DemoBitrateConfig)
    resolutions: List[DemoResolution] = Field(default_factory=_demo_default_resolutions)
    fps: List[DemoFps] = Field(default_factory=_demo_default_fps)
    codecs: List[DemoCodec] = Field(default_factory=_demo_default_codecs)
    camera_models: List[str] = Field(default_factory=_demo_default_camera_models)
    processing: DemoProcessingConfig = Field(default_factory=DemoProcessingConfig)
    output_ratio: DemoOutputRatioConfig = Field(default_factory=DemoOutputRatioConfig)
    errors: DemoErrorsConfig = Field(default_factory=DemoErrorsConfig)
    kept_original: DemoKeptOriginalConfig = Field(default_factory=DemoKeptOriginalConfig)
    discovery: DemoDiscoveryConfig = Field(default_factory=DemoDiscoveryConfig)

    @model_validator(mode="after")
    def validate_defaults(self):
        if not self.files.extensions:
            self.files.extensions = _demo_default_extensions()
        if not self.errors.types and self.errors.total > 0:
            self.errors.types = _demo_default_error_types()
        return self
