"""Helpers for bitrate-based quality mode parsing and validation."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, Literal, Optional

RateValueClass = Literal["absolute", "ratio"]

_RATE_PATTERN = re.compile(r"^(?P<number>\d+(?:\.\d+)?)(?P<suffix>[A-Za-z]*)$")
_SUFFIX_MULTIPLIERS: Dict[str, float] = {
    "": 1.0,
    "bps": 1.0,
    "k": 1_000.0,
    "kbps": 1_000.0,
    "m": 1_000_000.0,
    "mbps": 1_000_000.0,
    "g": 1_000_000_000.0,
    "gbps": 1_000_000_000.0,
}


@dataclass(frozen=True)
class ParsedRateValue:
    raw: str
    value_class: RateValueClass
    value: float


@dataclass(frozen=True)
class ResolvedRateControl:
    target_bps: int
    minrate_bps: Optional[int] = None
    maxrate_bps: Optional[int] = None


def _format_float(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def format_bps_human(bps: int) -> str:
    if bps >= 1_000_000:
        return f"{_format_float(bps / 1_000_000)} Mbps"
    if bps >= 1_000:
        return f"{_format_float(bps / 1_000)} kbps"
    return f"{bps} bps"


def describe_rate_target(value: Optional[str]) -> str:
    if not value:
        return "rate"
    parsed = parse_rate_value(value)
    if parsed.value_class == "absolute":
        return format_bps_human(int(round(parsed.value)))
    return f"input x{_format_float(parsed.value)}"


def parse_rate_value(raw_value: Any) -> ParsedRateValue:
    text = str(raw_value).strip()
    if not text:
        raise ValueError("Rate value cannot be empty.")

    compact = text.replace(" ", "")
    match = _RATE_PATTERN.fullmatch(compact)
    if not match:
        raise ValueError(
            f"Invalid rate value '{text}'. Use numeric bps or suffixes like k, M, Mbps."
        )

    number = float(match.group("number"))
    suffix = match.group("suffix").lower()
    if suffix not in _SUFFIX_MULTIPLIERS:
        raise ValueError(
            f"Unsupported bitrate suffix '{suffix}' in '{text}'. Supported: k, M, Mbps, bps."
        )

    if suffix == "" and 0 < number < 1:
        return ParsedRateValue(raw=text, value_class="ratio", value=number)

    bitrate_bps = number * _SUFFIX_MULTIPLIERS[suffix]
    if bitrate_bps <= 0:
        raise ValueError(f"Bitrate must be > 0 (got '{text}').")
    return ParsedRateValue(raw=text, value_class="absolute", value=bitrate_bps)


def parse_rate_fields(
    bps: Optional[str],
    minrate: Optional[str],
    maxrate: Optional[str],
) -> Dict[str, ParsedRateValue]:
    parsed: Dict[str, ParsedRateValue] = {}
    if bps is not None:
        parsed["bps"] = parse_rate_value(bps)
    if minrate is not None:
        parsed["minrate"] = parse_rate_value(minrate)
    if maxrate is not None:
        parsed["maxrate"] = parse_rate_value(maxrate)
    return parsed


def validate_rate_control_inputs(
    mode: str,
    bps: Optional[str],
    minrate: Optional[str],
    maxrate: Optional[str],
    *,
    allow_values_when_non_rate: bool,
) -> Dict[str, ParsedRateValue]:
    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in {"cq", "rate"}:
        raise ValueError(f"Invalid quality_mode '{mode}'. Use 'cq' or 'rate'.")

    parsed = parse_rate_fields(bps, minrate, maxrate)

    if normalized_mode != "rate":
        if not allow_values_when_non_rate and parsed:
            raise ValueError("bps/minrate/maxrate require quality_mode=rate.")
        return parsed

    if "bps" not in parsed:
        raise ValueError("quality_mode=rate requires bps.")

    classes = {entry.value_class for entry in parsed.values()}
    if len(classes) > 1:
        raise ValueError(
            "bps/minrate/maxrate must use the same numeric class (all absolute or all ratio)."
        )

    if "minrate" in parsed and "maxrate" in parsed and parsed["minrate"].value > parsed["maxrate"].value:
        raise ValueError("minrate must be <= maxrate.")
    if "minrate" in parsed and parsed["bps"].value < parsed["minrate"].value:
        raise ValueError("bps must be >= minrate.")
    if "maxrate" in parsed and parsed["bps"].value > parsed["maxrate"].value:
        raise ValueError("bps must be <= maxrate.")

    return parsed


def resolve_rate_value_bps(parsed: ParsedRateValue, source_bps: Optional[float]) -> int:
    if parsed.value_class == "absolute":
        return max(1, int(round(parsed.value)))
    if source_bps is None or source_bps <= 0:
        raise ValueError("Source bitrate unavailable; cannot resolve ratio-based bitrate.")
    return max(1, int(round(source_bps * parsed.value)))


def resolve_rate_control_values(
    bps: Optional[str],
    minrate: Optional[str],
    maxrate: Optional[str],
    source_bps: Optional[float],
) -> ResolvedRateControl:
    parsed = validate_rate_control_inputs(
        "rate",
        bps,
        minrate,
        maxrate,
        allow_values_when_non_rate=False,
    )

    target_bps = resolve_rate_value_bps(parsed["bps"], source_bps)
    minrate_bps = resolve_rate_value_bps(parsed["minrate"], source_bps) if "minrate" in parsed else None
    maxrate_bps = resolve_rate_value_bps(parsed["maxrate"], source_bps) if "maxrate" in parsed else None

    if minrate_bps is not None and maxrate_bps is not None and minrate_bps > maxrate_bps:
        raise ValueError("Resolved minrate is greater than resolved maxrate.")
    if minrate_bps is not None and target_bps < minrate_bps:
        raise ValueError("Resolved bps is below resolved minrate.")
    if maxrate_bps is not None and target_bps > maxrate_bps:
        raise ValueError("Resolved bps is above resolved maxrate.")

    return ResolvedRateControl(
        target_bps=target_bps,
        minrate_bps=minrate_bps,
        maxrate_bps=maxrate_bps,
    )
