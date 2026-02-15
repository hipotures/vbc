"""Configuration models for VBC application.

Pydantic models defining the YAML configuration schema with validation rules.
All fields have defaults; empty YAML files are valid (use all defaults).

Key models:
- AppConfig: Top-level configuration container
- GeneralConfig: Core compression settings (threads, GPU, filters, metadata)
- GpuConfig: GPU monitoring and sparkline display settings
- UiConfig: Dashboard layout and display preferences
- AutoRotateConfig: Filename pattern-based rotation rules

Configuration precedence: CLI args > YAML > defaults
"""

from typing import List, Dict, Optional, Union, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from vbc.config.rate_control import validate_rate_control_inputs

QUEUE_SORT_CHOICES = ("name", "rand", "dir", "size", "size-asc", "size-desc", "ext")
QUEUE_SORT_ALIASES = {"size": "size-asc"}


def normalize_queue_sort(value: Optional[str]) -> str:
    """Normalize and validate queue sorting mode.

    Args:
        value: Raw queue_sort value from config or CLI.

    Returns:
        Normalized queue_sort mode ("name", "rand", "dir", "size-asc", "size-desc", "ext").

    Raises:
        ValueError: If value is not a valid queue_sort choice.
    """
    if value is None:
        return "name"
    normalized = str(value).strip().lower()
    if not normalized:
        return "name"
    normalized = QUEUE_SORT_ALIASES.get(normalized, normalized)
    if normalized not in QUEUE_SORT_CHOICES:
        allowed = ", ".join(QUEUE_SORT_CHOICES)
        raise ValueError(f"Invalid queue_sort '{value}'. Use one of: {allowed}.")
    return normalized


def validate_queue_sort(value: Optional[str], extensions: List[str]) -> str:
    """Validate queue_sort mode with extension dependency check.

    Args:
        value: Queue sort mode to validate.
        extensions: List of file extensions (required for 'ext' mode).

    Returns:
        Validated queue_sort mode.

    Raises:
        ValueError: If 'ext' mode is used without extensions list.
    """
    normalized = normalize_queue_sort(value)
    if normalized == "ext" and not extensions:
        raise ValueError("queue_sort 'ext' requires a non-empty extensions list.")
    return normalized


def _default_gpu_common_args() -> List[str]:
    return [
        "-c:v av1_nvenc",
        "-preset p7",
        "-tune hq",
        "-b:v 0",
        "-cq 45",
        "-f mp4",
    ]


def _default_gpu_advanced_args() -> List[str]:
    return [
        "-c:v av1_nvenc",
        "-preset p7",
        "-tune hq",
        "-b:v 0",
        "-cq 45",
        "-rc vbr",
        "-multipass fullres",
        "-rc-lookahead 32",
        "-spatial-aq 1",
        "-temporal-aq 1",
        "-aq-strength 8",
        "-b_ref_mode middle",
        "-f mp4",
    ]


def _default_cpu_common_args() -> List[str]:
    return [
        "-c:v libsvtav1",
        "-preset 6",
        "-crf 32",
        "-svtav1-params tune=0:enable-overlays=1",
        "-f mp4",
    ]


def _default_cpu_advanced_args() -> List[str]:
    return [
        "-c:v libaom-av1",
        "-crf 30",
        "-b:v 0",
        "-cpu-used 0",
        "-tune ssim",
        "-lag-in-frames 35",
        "-aq-mode 1",
        "-row-mt 1",
        "-threads 0",
        "-f matroska",
    ]


class GpuEncoderConfig(BaseModel):
    """NVENC AV1 encoder settings."""

    advanced: bool = False
    common_args: List[str] = Field(default_factory=_default_gpu_common_args)
    advanced_args: List[str] = Field(default_factory=_default_gpu_advanced_args)


class CpuEncoderConfig(BaseModel):
    """CPU encoder settings for SVT-AV1 and advanced AOM modes."""

    advanced: bool = False
    common_args: List[str] = Field(default_factory=_default_cpu_common_args)
    advanced_args: List[str] = Field(default_factory=_default_cpu_advanced_args)
    advanced_enforce_input_pix_fmt: bool = True


class GpuConfig(BaseModel):
    """GPU monitoring and dashboard sparkline configuration.

    Controls GPU metrics sampling and visualization. Metrics include:
    utilization, memory usage, temperature, and power draw.

    Attributes:
        enabled: Enable GPU monitoring and sparkline display.
        refresh_rate: [DEPRECATED] Use sample_interval_s instead.
        sample_interval_s: Seconds between GPU metric samples (min 0.1s).
        history_window_s: Total time window shown in sparklines (min 10s, default 5min).
        nvtop_device_index: GPU index to monitor (default 0 for primary GPU).
        nvtop_device_name: Override device selection by name instead of index.
        nvtop_path: Custom path to nvtop binary (e.g., /usr/local/bin/nvtop). Auto-detected if not set.
    """

    enabled: bool = True
    refresh_rate: int = Field(default=5, ge=1)
    sample_interval_s: float = Field(default=5.0, ge=0.1)
    history_window_s: float = Field(default=300.0, ge=10.0)
    nvtop_device_index: int = Field(default=0, ge=0)
    nvtop_device_name: Optional[str] = None
    nvtop_path: Optional[str] = None


class DynamicRateConfig(BaseModel):
    """Per-camera bitrate rule for quality_mode='rate'."""

    bps: str
    minrate: Optional[str] = None
    maxrate: Optional[str] = None

    @field_validator("bps", "minrate", "maxrate")
    @classmethod
    def normalize_rate_value(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = str(v).strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_rate_fields(self):
        validate_rate_control_inputs(
            "rate",
            self.bps,
            self.minrate,
            self.maxrate,
            allow_values_when_non_rate=False,
        )
        return self


class DynamicQualityRule(BaseModel):
    """Per-camera dynamic quality rule."""

    cq: int = Field(ge=0, le=63)
    rate: Optional[DynamicRateConfig] = None


class GeneralConfig(BaseModel):
    """Core compression and processing configuration.

    Controls threading, GPU/CPU selection, file filtering, and metadata handling.
    Quality targets can be controlled by CQ/CRF (default) or bitrate mode.

    Attributes:
        threads: Max concurrent compression jobs (default 1, min 1).
        prefetch_factor: Submit-on-demand multiplier (jobs = threads * prefetch_factor).
        gpu: Use GPU (NVENC) instead of CPU (SVT-AV1) encoder.
        gpu_refresh_rate: [DEPRECATED] Use gpu_config.sample_interval_s.
        queue_sort: Queue sorting mode (name, rand, dir, size-asc, size-desc, ext).
        queue_seed: Random seed for deterministic 'rand' sorting (None = random).
        log_path: Path to FFmpeg log file (None = no logging).
        cpu_fallback: Retry with CPU if GPU hardware capability exceeded.
        ffmpeg_cpu_threads: Limit threads per FFmpeg worker (None = FFmpeg decides).
        copy_metadata: Preserve EXIF/XMP metadata from source video.
        use_exif: Extract camera model from EXIF for dynamic_quality matching.
        filter_cameras: Only process videos from these camera models (empty = all).
        dynamic_quality: Per-camera rules (e.g., {"ILCE-7RM5": {"cq": 38}}).
        quality_mode: Rate control mode: "cq" (default) or "rate" (bitrate).
        bps: Target bitrate value for rate mode (absolute or ratio).
        minrate: Optional minimum bitrate for rate mode (same class as bps).
        maxrate: Optional maximum bitrate for rate mode (same class as bps).
        extensions: Video file extensions to process.
        min_size_bytes: Skip files smaller than this (default 1MiB).
        clean_errors: Remove .err markers and retry failed jobs.
        skip_av1: Skip files already encoded in AV1 codec.
        strip_unicode_display: Remove unicode chars from displayed filenames (UI safety).
        manual_rotation: Force rotation angle (0, 90, 180, 270) for all videos (None = auto).
        min_compression_ratio: Minimum savings required (0.1 = 10%; keep original if below).
        debug: Enable verbose logging and timing information.
    """

    threads: int = Field(default=1, gt=0)
    prefetch_factor: int = Field(default=1, ge=1)
    gpu: bool = True
    gpu_refresh_rate: int = Field(default=5, ge=1)
    queue_sort: str = Field(default="name")
    queue_seed: Optional[int] = Field(default=None)
    log_path: Optional[str] = Field(default="/tmp/vbc/compression.log")
    cpu_fallback: bool = Field(default=False)
    ffmpeg_cpu_threads: Optional[int] = Field(default=None, ge=1)
    copy_metadata: bool = True
    use_exif: bool = True
    filter_cameras: List[str] = Field(default_factory=list)
    dynamic_quality: Dict[str, DynamicQualityRule] = Field(default_factory=dict)
    quality_mode: Literal["cq", "rate"] = "cq"
    bps: Optional[str] = None
    minrate: Optional[str] = None
    maxrate: Optional[str] = None
    extensions: List[str] = Field(default_factory=lambda: [".mp4", ".mov", ".avi", ".flv", ".webm"])
    min_size_bytes: int = Field(default=1048576)
    clean_errors: bool = False
    skip_av1: bool = False
    strip_unicode_display: bool = True
    manual_rotation: Optional[int] = Field(default=None)
    min_compression_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    repair_corrupted_flv: bool = False
    debug: bool = False

    @field_validator("queue_sort")
    @classmethod
    def validate_queue_sort_mode(cls, v: Optional[str]) -> str:
        return normalize_queue_sort(v)

    @field_validator("quality_mode", mode="before")
    @classmethod
    def normalize_quality_mode(cls, v: str) -> str:
        return str(v).strip().lower()

    @field_validator("log_path")
    @classmethod
    def normalize_log_path(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = str(v).strip()
        return cleaned or None

    @field_validator("bps", "minrate", "maxrate")
    @classmethod
    def normalize_rate_value(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = str(v).strip()
        return cleaned or None

    @field_validator("dynamic_quality", mode="before")
    @classmethod
    def validate_dynamic_quality_schema(cls, v):
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("dynamic_quality must be a mapping of camera pattern -> rule.")

        for pattern, rule in v.items():
            if isinstance(rule, DynamicQualityRule):
                continue
            if isinstance(rule, int):
                raise ValueError(
                    f"Legacy dynamic_quality format is not supported for '{pattern}'. "
                    "Use object format: {\"pattern\": {\"cq\": 35}}."
                )
            if not isinstance(rule, dict):
                raise ValueError(
                    f"Invalid dynamic_quality rule for '{pattern}'. "
                    "Expected mapping with required 'cq' and optional 'rate'."
                )

        return v

    @model_validator(mode="after")
    def validate_queue_sort_dependencies(self):
        self.queue_sort = validate_queue_sort(self.queue_sort, self.extensions)
        validate_rate_control_inputs(
            self.quality_mode,
            self.bps,
            self.minrate,
            self.maxrate,
            allow_values_when_non_rate=True,
        )
        return self

class AutoRotateConfig(BaseModel):
    """Filename pattern-based automatic rotation configuration.

    Maps regex patterns to rotation angles. If a filename matches a pattern,
    that rotation is applied automatically (overrides manual_rotation).

    Attributes:
        patterns: Dict mapping regex patterns to angles (0, 90, 180, 270).
                  Example: {"DJI_.*\\.MP4": 0, "GOPR.*\\.MP4": 180}
    """

    patterns: Dict[str, int] = Field(default_factory=dict)

    @field_validator('patterns')
    @classmethod
    def validate_angles(cls, v: Dict[str, int]) -> Dict[str, int]:
        """Validate that all rotation angles are 0, 90, 180, or 270 degrees."""
        for pattern, angle in v.items():
            if angle not in {0, 90, 180, 270}:
                raise ValueError(f"Invalid rotation angle {angle} for pattern {pattern}. Must be 0, 90, 180, or 270.")
        return v


class UiConfig(BaseModel):
    """Dashboard UI display configuration.

    Controls Rich dashboard layout, panel sizing, and display limits.

    Attributes:
        activity_feed_max_items: Max items in activity feed panel (1-20, default 5).
        active_jobs_max_display: Max concurrent jobs to show in panel (1-16, default 8).
        panel_height_scale: Dashboard height fraction (0.3-1.0, default 0.7 = 30% reduction).
    """

    activity_feed_max_items: int = Field(default=5, ge=1, le=20)
    active_jobs_max_display: int = Field(default=8, ge=1, le=16)
    panel_height_scale: float = Field(default=0.7, ge=0.3, le=1.0)


class AppConfig(BaseModel):
    """Top-level VBC application configuration.

    Combines all configuration sections: general, GPU monitoring, encoder, UI, autorotate, and directory mappings.

    Directory mapping modes:
    1. Suffix mode: input_dirs + suffix_output_dirs (e.g., /videos -> /videos_out)
    2. Explicit mode: input_dirs[i] -> output_dirs[i] (1:1 pairing required)

    Attributes:
        general: Core compression settings.
        input_dirs: List of input directories to scan (empty = must provide via CLI).
        output_dirs: List of output directories (empty = use suffix mode).
        suffix_output_dirs: Suffix for auto-generated output dirs (default "_out").
        errors_dirs: List of .err marker directories (empty = use suffix mode).
        suffix_errors_dirs: Suffix for auto-generated error dirs (default "_err").
        autorotate: Filename pattern-based rotation rules.
        gpu_config: GPU monitoring configuration.
        gpu_encoder: GPU encoder configuration.
        cpu_encoder: CPU encoder configuration.
        ui: Dashboard UI configuration.
    """

    general: GeneralConfig
    input_dirs: List[str] = Field(default_factory=list)
    output_dirs: List[str] = Field(default_factory=list)
    suffix_output_dirs: Optional[str] = Field(default="_out")
    errors_dirs: List[str] = Field(default_factory=list)
    suffix_errors_dirs: Optional[str] = Field(default="_err")
    autorotate: AutoRotateConfig = Field(default_factory=AutoRotateConfig)
    gpu_config: GpuConfig = Field(default_factory=GpuConfig)
    gpu_encoder: GpuEncoderConfig = Field(default_factory=GpuEncoderConfig)
    cpu_encoder: CpuEncoderConfig = Field(default_factory=CpuEncoderConfig)
    ui: UiConfig = Field(default_factory=UiConfig)

    @field_validator("suffix_output_dirs")
    @classmethod
    def normalize_suffix_output_dirs(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = str(v).strip()
        return cleaned or None

    @field_validator("suffix_errors_dirs")
    @classmethod
    def normalize_suffix_errors_dirs(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = str(v).strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_output_dir_settings(self):
        if self.output_dirs and self.suffix_output_dirs:
            raise ValueError("output_dirs cannot be used with suffix_output_dirs.")
        if not self.output_dirs and not self.suffix_output_dirs:
            raise ValueError("suffix_output_dirs must be set when output_dirs is empty.")
        if self.errors_dirs and self.suffix_errors_dirs:
            raise ValueError("errors_dirs cannot be used with suffix_errors_dirs.")
        if not self.errors_dirs and not self.suffix_errors_dirs:
            raise ValueError("suffix_errors_dirs must be set when errors_dirs is empty.")
        return self

class DemoExtension(BaseModel):
    ext: str
    weight: float = Field(default=1.0, gt=0)

class DemoFilesConfig(BaseModel):
    count: Optional[int] = Field(default=None, ge=0)
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

class DemoInputFolder(BaseModel):
    """Demo input folder with mockup status and stats.

    Attributes:
        name: Folder path/name (e.g., "DEMO/Studio_A").
        status: Folder status - "ok", "nonexist", "norw" (None defaults to "ok").
        files: Number of files in folder (mockup data for demo display).
        size: Folder size as string (e.g., "10MB", "1.5GB") for demo display.
    """
    name: str
    status: Optional[str] = None
    files: Optional[int] = None
    size: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        allowed = {"ok", "nonexist", "norw"}
        if v not in allowed:
            raise ValueError(f"Invalid folder status '{v}'. Use one of: {', '.join(sorted(allowed))}")
        return v

class DemoConfig(BaseModel):
    seed: Optional[int] = None
    input_folders: List[Union[str, DemoInputFolder]] = Field(default_factory=lambda: ["DEMO/Studio_A", "DEMO/Studio_B"])
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
