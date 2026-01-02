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


DEFAULT_GPU_SPARKLINE_STYLE = SparklineStyle(
    name="classic_8",
    blocks="▁▂▃▄▅▆▇█",
    missing="·",
)

GPU_SPARKLINE_PRESETS = {
    "classic_8": SparklineConfig(
        style=DEFAULT_GPU_SPARKLINE_STYLE,
        label="Default",
        metrics=[
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
        ],
    ),
}

DEFAULT_GPU_SPARKLINE_PRESET = "classic_8"


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
) -> str:
    """Render sparkline with newest on right, missing as style.missing."""
    if spark_len <= 0:
        return ""

    samples = list(history)[-spark_len:]  # Last N samples (oldest -> newest)
    chars = []
    for val in samples:
        bin_idx = bin_value(val, min_val, max_val, style.num_bins)
        if bin_idx < 0 or not style.blocks:
            chars.append(style.missing)
        else:
            chars.append(style.blocks[bin_idx])

    result = "".join(chars)
    if len(result) < spark_len:
        result = result + " " * (spark_len - len(result))

    return result


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
