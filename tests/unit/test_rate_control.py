import pytest

from vbc.config.rate_control import (
    parse_rate_value,
    parse_rate_cap_bps,
    validate_rate_control_inputs,
    resolve_rate_control_values,
    format_bps_human,
)


@pytest.mark.parametrize(
    ("raw", "expected_bps"),
    [
        ("200000000", 200000000),
        ("200000k", 200000000),
        ("200M", 200000000),
        ("200Mbps", 200000000),
        ("200mbps", 200000000),
    ],
)
def test_parse_rate_value_absolute_formats(raw, expected_bps):
    parsed = parse_rate_value(raw)
    assert parsed.value_class == "absolute"
    assert int(round(parsed.value)) == expected_bps


def test_parse_rate_value_ratio():
    parsed = parse_rate_value("0.8")
    assert parsed.value_class == "ratio"
    assert parsed.value == 0.8


def test_parse_rate_cap_bps_accepts_absolute():
    assert parse_rate_cap_bps("95M", field_name="rate_target_max_bps") == 95_000_000


def test_parse_rate_cap_bps_rejects_ratio():
    with pytest.raises(ValueError):
        parse_rate_cap_bps("0.8", field_name="rate_target_max_bps")


def test_validate_rate_control_rejects_mixed_numeric_classes():
    with pytest.raises(ValueError):
        validate_rate_control_inputs(
            "rate",
            "0.8",
            "0.7",
            "220000000",
            allow_values_when_non_rate=True,
        )


def test_resolve_ratio_rate_control_uses_source_bitrate():
    resolved = resolve_rate_control_values(
        bps="0.8",
        minrate="0.7",
        maxrate="0.9",
        source_bps=200000000,
    )
    assert resolved.target_bps == 160000000
    assert resolved.minrate_bps == 140000000
    assert resolved.maxrate_bps == 180000000


def test_format_bps_human_mbps():
    assert format_bps_human(200000000) == "200 Mbps"
