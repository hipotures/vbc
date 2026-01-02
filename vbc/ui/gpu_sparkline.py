from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class SparklineStyle:
    name: str
    blocks: str
    missing: str = "·"

    @property
    def num_bins(self) -> int:
        return max(1, len(self.blocks))


@dataclass(frozen=True)
class SparklineMetricConfig:
    label: str
    history_attr: str
    min_val: float
    max_val: float
    unit: str
    display_name: Optional[str] = None
    cycle_label: Optional[str] = None
    legend_group: Optional[str] = None

    @property
    def cycle_name(self) -> str:
        return self.cycle_label or self.label.lower()

    @property
    def legend_name(self) -> str:
        return self.legend_group or self.cycle_name

    @property
    def display_label(self) -> str:
        return self.display_name or self.label


@dataclass(frozen=True)
class SparklineConfig:
    style: SparklineStyle
    metrics: Sequence[SparklineMetricConfig]
    label: Optional[str] = None


@dataclass(frozen=True)
class SparklinePalette:
    name: str
    colors: Sequence[str]
    label: Optional[str] = None

    @property
    def display_label(self) -> str:
        return self.label or self.name


DEFAULT_GPU_SPARKLINE_STYLE = SparklineStyle(
    name="classic_8",
    blocks="▁▂▃▄▅▆▇█",
    missing="·",
)

_GPU_SPARKLINE_METRICS = (
    # Order matches GL2 display: temp | fan | pwr | gpu | mem
    SparklineMetricConfig(
        label="Temp",
        history_attr="gpu_history_temp",
        min_val=35.0,
        max_val=70.0,
        unit="°C",
        display_name="Temperature",
    ),
    SparklineMetricConfig(
        label="Fan",
        history_attr="gpu_history_fan",
        min_val=0.0,
        max_val=100.0,
        unit="%",
        display_name="Fan Speed",
        legend_group="%",
    ),
    SparklineMetricConfig(
        label="Pwr",
        history_attr="gpu_history_pwr",
        min_val=100.0,
        max_val=400.0,
        unit="W",
        display_name="Power Draw",
    ),
    SparklineMetricConfig(
        label="GPU",
        history_attr="gpu_history_gpu",
        min_val=0.0,
        max_val=100.0,
        unit="%",
        display_name="GPU Utilization",
        legend_group="%",
    ),
    SparklineMetricConfig(
        label="Mem",
        history_attr="gpu_history_mem",
        min_val=0.0,
        max_val=100.0,
        unit="%",
        display_name="Memory Utilization",
        legend_group="%",
    ),
)

GPU_SPARKLINE_PRESETS = {
    "classic_8": SparklineConfig(
        style=DEFAULT_GPU_SPARKLINE_STYLE,
        label="Classic",
        metrics=_GPU_SPARKLINE_METRICS,
    ),
    "shade_4": SparklineConfig(
        style=SparklineStyle(name="shade_4", blocks="░▒▓█"),
        label="Shade",
        metrics=_GPU_SPARKLINE_METRICS,
    ),
    "dots_5": SparklineConfig(
        style=SparklineStyle(name="dots_5", blocks="○◔◑◕●"),
        label="Dots",
        metrics=_GPU_SPARKLINE_METRICS,
    ),
    "digits_10": SparklineConfig(
        style=SparklineStyle(name="digits_10", blocks="0123456789"),
        label="Digits",
        metrics=_GPU_SPARKLINE_METRICS,
    ),
    "hex_16": SparklineConfig(
        style=SparklineStyle(name="hex_16", blocks="0123456789ABCDEF"),
        label="Hex",
        metrics=_GPU_SPARKLINE_METRICS,
    ),
}

DEFAULT_GPU_SPARKLINE_PRESET = "classic_8"

GPU_SPARKLINE_PALETTES = {
    "mocha": SparklinePalette(
        name="mocha",
        colors=[
            "#4E3A2F",
            "#5F4230",
            "#6F4A32",
            "#805233",
            "#905A35",
            "#9A6540",
            "#A4704B",
            "#AD7A55",
            "#B78560",
            "#C1916B",
            "#CA9C76",
            "#D3A881",
            "#DCB38D",
            "#E4BE99",
            "#EDC8A6",
            "#F5D3B2",
        ],
    ),
    "viridis": SparklinePalette(
        name="viridis",
        colors=[
            "#440154",
            "#481A6C",
            "#472F7D",
            "#414487",
            "#39568C",
            "#31688E",
            "#2A788E",
            "#23888E",
            "#1F988B",
            "#22A884",
            "#35B779",
            "#54C568",
            "#7AD151",
            "#A5DB36",
            "#D2E21B",
            "#FDE725",
        ],
    ),
    "plasma": SparklinePalette(
        name="plasma",
        colors=[
            "#0D0887",
            "#330597",
            "#5002A2",
            "#6A00A8",
            "#8305A7",
            "#9A179B",
            "#AE2891",
            "#C03A83",
            "#CF4B76",
            "#DD5E66",
            "#E97158",
            "#F1854B",
            "#F99A3E",
            "#FBBF2B",
            "#F9DC24",
            "#F0F921",
        ],
    ),
    "cividis": SparklinePalette(
        name="cividis",
        colors=[
            "#00224E",
            "#002E6C",
            "#1E3A6F",
            "#3B496C",
            "#525865",
            "#666760",
            "#7A765A",
            "#8F8554",
            "#A3944E",
            "#B7A448",
            "#CAB341",
            "#DCC239",
            "#EBD136",
            "#F4E138",
            "#FEE838",
            "#FEE838",
        ],
    ),
}

DEFAULT_GPU_SPARKLINE_PALETTE = "mocha"
PALETTE_GLYPH = "█"


def get_gpu_sparkline_config(preset: Optional[str] = None) -> SparklineConfig:
    if preset and preset in GPU_SPARKLINE_PRESETS:
        return GPU_SPARKLINE_PRESETS[preset]
    return GPU_SPARKLINE_PRESETS[DEFAULT_GPU_SPARKLINE_PRESET]


def list_gpu_sparkline_presets() -> List[str]:
    return list(GPU_SPARKLINE_PRESETS.keys())


def format_preset_label(preset: str, config: SparklineConfig) -> str:
    label = config.label or preset.replace("_", " ").title()
    blocks = config.style.blocks
    if blocks:
        return f"{label} {blocks}"
    return label


def get_gpu_sparkline_palette(name: Optional[str] = None) -> SparklinePalette:
    if name and name in GPU_SPARKLINE_PALETTES:
        return GPU_SPARKLINE_PALETTES[name]
    return GPU_SPARKLINE_PALETTES[DEFAULT_GPU_SPARKLINE_PALETTE]


def list_gpu_sparkline_palettes() -> List[str]:
    return list(GPU_SPARKLINE_PALETTES.keys())


def bin_value(val: Optional[float], min_val: float, max_val: float, num_bins: int) -> int:
    """Map value to 0..(num_bins-1) for sparkline block char. -1 for None."""
    if val is None:
        return -1
    if num_bins <= 1 or max_val <= min_val:
        return 0
    if val <= min_val:
        return 0
    if val >= max_val:
        return num_bins - 1
    ratio = (val - min_val) / (max_val - min_val)
    return min(num_bins - 1, int(ratio * num_bins))


def render_sparkline(
    history: Iterable[Optional[float]],
    spark_len: int,
    min_val: float,
    max_val: float,
    style: SparklineStyle,
    palette: Optional[Sequence[str]] = None,
    glyph: Optional[str] = None,
) -> str:
    """Render sparkline with newest on right, missing as style.missing."""
    if spark_len <= 0:
        return ""

    samples = list(history)[-spark_len:]  # Last N samples (oldest -> newest)
    chars: List[str] = []
    visible_len = 0
    for val in samples:
        bin_idx = bin_value(val, min_val, max_val, style.num_bins)
        if bin_idx < 0 or not style.blocks:
            char = style.missing
            if palette:
                chars.append(f"[dim]{char}[/]")
            else:
                chars.append(char)
        else:
            char = glyph or style.blocks[bin_idx]
            if palette:
                color = _palette_color_for_value(val, min_val, max_val, palette)
                chars.append(f"[{color}]{char}[/]")
            else:
                chars.append(char)
        visible_len += 1

    if visible_len < spark_len:
        chars.append(" " * (spark_len - visible_len))

    return "".join(chars)


def build_palette_preview(style: SparklineStyle, palette: Sequence[str]) -> str:
    if not style.blocks:
        return ""
    if not palette:
        return style.blocks
    preview = []
    for idx, char in enumerate(style.blocks):
        color = _palette_color_for_bin(idx, style.num_bins, palette)
        preview.append(f"[{color}]{char}[/]")
    return "".join(preview)


def build_palette_swatches(
    palette: Sequence[str],
    step: int = 2,
    block: str = "█",
    separator: str = "",
) -> str:
    if not palette:
        return ""
    colors = list(palette)[::step]
    swatches = [f"[{color}]{block}[/]" for color in colors]
    return separator.join(swatches)


def _palette_color_for_bin(bin_idx: int, num_bins: int, palette: Sequence[str]) -> str:
    if not palette:
        return "white"
    if len(palette) == 1 or num_bins <= 1:
        return palette[0]
    scaled = int((bin_idx * (len(palette) - 1)) / max(1, (num_bins - 1)))
    scaled = max(0, min(scaled, len(palette) - 1))
    return palette[scaled]


def _palette_color_for_value(
    value: float,
    min_val: float,
    max_val: float,
    palette: Sequence[str],
) -> str:
    if not palette:
        return "white"
    if len(palette) == 1 or max_val <= min_val:
        return palette[0]
    clamped = min(max(value, min_val), max_val)
    ratio = (clamped - min_val) / (max_val - min_val)
    scaled = int(ratio * (len(palette) - 1))
    scaled = max(0, min(scaled, len(palette) - 1))
    return palette[scaled]


def build_cycle_text(metrics: Sequence[SparklineMetricConfig]) -> str:
    return " → ".join(metric.cycle_name for metric in metrics)


def build_scale_entries(metrics: Sequence[SparklineMetricConfig]) -> List[str]:
    entries: List[dict] = []
    seen = {}

    for metric in metrics:
        if metric.legend_group:
            key = ("group", metric.legend_group, metric.min_val, metric.max_val, metric.unit)
            label = metric.legend_group
            grouped = True
        else:
            key = ("range", metric.min_val, metric.max_val, metric.unit)
            label = metric.cycle_name
            grouped = False

        if key not in seen:
            entry = {
                "labels": [label],
                "min": metric.min_val,
                "max": metric.max_val,
                "unit": metric.unit,
                "grouped": grouped,
            }
            entries.append(entry)
            seen[key] = entry
        else:
            entry = seen[key]
            if not entry["grouped"] and label not in entry["labels"]:
                entry["labels"].append(label)

    formatted = []
    for entry in entries:
        if entry["grouped"]:
            label = entry["labels"][0]
        else:
            label = "/".join(entry["labels"])
        formatted.append(f"{label}: {format_range(entry['min'], entry['max'], entry['unit'])}")

    return formatted


def format_range(min_val: float, max_val: float, unit: str) -> str:
    min_str = format_value(min_val)
    max_str = format_value(max_val)
    if unit == "%":
        return f"{min_str}..{max_str}{unit}"
    if unit:
        return f"{min_str}{unit}..{max_str}{unit}"
    return f"{min_str}..{max_str}"


def format_value(val: float) -> str:
    if float(val).is_integer():
        return str(int(val))
    return f"{val:.1f}".rstrip("0").rstrip(".")
