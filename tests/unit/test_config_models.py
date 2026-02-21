import pytest
import yaml
from pydantic import ValidationError
from vbc.config.models import AppConfig, GeneralConfig, AutoRotateConfig

def test_valid_config():
    data = {
        "general": {
            "threads": 4,
            "gpu": True,
            "copy_metadata": True,
            "use_exif": True,
            "filter_cameras": ["Sony"],
            "dynamic_quality": {"Sony": {"cq": 35}},
            "extensions": [".mp4"],
            "min_size_bytes": 1024
        },
        "autorotate": {
            "patterns": {"QVR.*": 180}
        }
    }
    config = AppConfig(**data)
    assert config.general.threads == 4
    assert config.autorotate.patterns["QVR.*"] == 180

def test_invalid_threads():
    with pytest.raises(ValidationError):
        GeneralConfig(threads=0, gpu=True, copy_metadata=True, use_exif=True, extensions=[".mp4"], min_size_bytes=0)

def test_config_defaults():
    gen = GeneralConfig(threads=1, extensions=[".mp4"])
    config = AppConfig(general=gen)
    assert gen.filter_cameras == []
    assert gen.dynamic_quality == {}
    assert gen.min_compression_ratio == 0.1
    assert gen.queue_sort == "name"
    assert gen.log_path == "/tmp/vbc/compression.log"
    assert gen.cpu_fallback is False
    assert gen.ffmpeg_cpu_threads is None
    assert gen.verify_fail_action == "false"
    assert "-cq 45" in config.gpu_encoder.common_args
    assert "-crf 32" in config.cpu_encoder.common_args
    assert config.errors_dirs == []
    assert config.suffix_errors_dirs == "_err"


def test_input_dirs_accepts_object_format():
    config = AppConfig(
        general=GeneralConfig(threads=1, extensions=[".mp4"]),
        input_dirs=[
            {"path": "/tmp/in_a", "enabled": True},
            {"path": "/tmp/in_b", "enabled": False},
        ],
    )
    assert config.input_dirs[0].path == "/tmp/in_a"
    assert config.input_dirs[0].enabled is True
    assert config.input_dirs[1].enabled is False


def test_input_dirs_rejects_duplicates():
    with pytest.raises(ValidationError):
        AppConfig(
            general=GeneralConfig(threads=1, extensions=[".mp4"]),
            input_dirs=[
                {"path": "/tmp/in_a", "enabled": True},
                {"path": "/tmp/in_a", "enabled": False},
            ],
        )


def test_output_dirs_conflict_with_suffix():
    general = GeneralConfig(threads=1, extensions=[".mp4"])
    with pytest.raises(ValidationError):
        AppConfig(
            general=general,
            output_dirs=["/tmp/out"],
            suffix_output_dirs="_out",
        )


def test_suffix_missing_without_output_dirs():
    general = GeneralConfig(threads=1, extensions=[".mp4"])
    with pytest.raises(ValidationError):
        AppConfig(
            general=general,
            output_dirs=[],
            suffix_output_dirs=None,
        )


def test_errors_dirs_conflict_with_suffix():
    general = GeneralConfig(threads=1, extensions=[".mp4"])
    with pytest.raises(ValidationError):
        AppConfig(
            general=general,
            errors_dirs=["/tmp/errors"],
            suffix_errors_dirs="_err",
        )


def test_suffix_missing_without_errors_dirs():
    general = GeneralConfig(threads=1, extensions=[".mp4"])
    with pytest.raises(ValidationError):
        AppConfig(
            general=general,
            errors_dirs=[],
            suffix_errors_dirs=None,
        )


def test_queue_sort_alias_size():
    gen = GeneralConfig(threads=1, extensions=[".mp4"], queue_sort="size")
    assert gen.queue_sort == "size-asc"


def test_queue_sort_invalid_mode():
    with pytest.raises(ValidationError):
        GeneralConfig(threads=1, extensions=[".mp4"], queue_sort="bad")


def test_queue_sort_ext_requires_extensions():
    with pytest.raises(ValidationError):
        GeneralConfig(threads=1, extensions=[], queue_sort="ext")


def test_ffmpeg_cpu_threads_requires_positive_value():
    with pytest.raises(ValidationError):
        GeneralConfig(threads=1, extensions=[".mp4"], ffmpeg_cpu_threads=0)


def test_rate_mode_requires_bps():
    with pytest.raises(ValidationError):
        GeneralConfig(threads=1, extensions=[".mp4"], quality_mode="rate")


def test_verify_fail_action_invalid_mode():
    with pytest.raises(ValidationError):
        GeneralConfig(threads=1, extensions=[".mp4"], verify_fail_action="halt")


def test_rate_mode_rejects_mixed_classes():
    with pytest.raises(ValidationError):
        GeneralConfig(
            threads=1,
            extensions=[".mp4"],
            quality_mode="rate",
            bps="0.8",
            minrate="220000000",
        )


def test_rate_mode_accepts_absolute_values():
    config = GeneralConfig(
        threads=1,
        extensions=[".mp4"],
        quality_mode="rate",
        bps="200Mbps",
        minrate="150M",
        maxrate="220M",
    )
    assert config.quality_mode == "rate"
    assert config.bps == "200Mbps"


def test_dynamic_quality_rejects_legacy_scalar_format():
    with pytest.raises(ValidationError):
        GeneralConfig(
            threads=1,
            extensions=[".mp4"],
            dynamic_quality={"Sony": 35},
        )


def test_dynamic_quality_accepts_rate_rule():
    config = GeneralConfig(
        threads=1,
        extensions=[".mp4"],
        dynamic_quality={
            "Sony": {
                "cq": 35,
                "rate": {
                    "bps": "0.8",
                    "minrate": "0.7",
                    "maxrate": "0.9",
                },
            }
        },
    )
    assert config.dynamic_quality["Sony"].cq == 35
    assert config.dynamic_quality["Sony"].rate is not None
    assert config.dynamic_quality["Sony"].rate.bps == "0.8"

def test_load_config(tmp_path):
    d = tmp_path / "conf"
    d.mkdir()
    f = d / "vbc.yaml"
    f.write_text("""
general:
  threads: 8
gpu_encoder:
  advanced: false
  common_args:
    - "-c:v av1_nvenc"
    - "-cq 30"
    - "-f mp4"
cpu_encoder:
  advanced: false
  common_args:
    - "-c:v libsvtav1"
    - "-crf 30"
    - "-f mp4"
autorotate:
  patterns:
    "test.*": 90
""")
    from vbc.config.loader import load_config
    config = load_config(f)
    assert config.general.threads == 8
    assert "-cq 30" in config.gpu_encoder.common_args
    assert config.autorotate.patterns["test.*"] == 90


def test_load_config_rejects_legacy_input_dirs_format(tmp_path):
    d = tmp_path / "conf"
    d.mkdir()
    f = d / "vbc.yaml"
    f.write_text(
        """
general: {}
input_dirs:
  - /tmp/a
"""
    )
    from vbc.config.loader import load_config
    with pytest.raises(ValueError, match="Legacy input_dirs list\\[str\\] format"):
        load_config(f)


def test_load_config_rejects_disabled_input_dirs_key(tmp_path):
    d = tmp_path / "conf"
    d.mkdir()
    f = d / "vbc.yaml"
    f.write_text(
        """
general: {}
input_dirs:
  - path: /tmp/a
    enabled: true
disabled_input_dirs:
  - /tmp/b
"""
    )
    from vbc.config.loader import load_config
    with pytest.raises(ValueError, match="disabled_input_dirs"):
        load_config(f)


def test_save_dirs_config_writes_unified_input_dirs(tmp_path):
    d = tmp_path / "conf"
    d.mkdir()
    f = d / "vbc.yaml"
    f.write_text(
        """
general: {}
input_dirs:
  - path: /tmp/old
    enabled: true
disabled_input_dirs:
  - /tmp/legacy
"""
    )
    from vbc.config.loader import save_dirs_config

    save_dirs_config(
        f,
        [
            {"path": "/tmp/a", "enabled": True},
            {"path": "/tmp/b", "enabled": False},
        ],
    )

    data = yaml.safe_load(f.read_text())
    assert data["input_dirs"] == [
        {"path": "/tmp/a", "enabled": True},
        {"path": "/tmp/b", "enabled": False},
    ]
    assert "disabled_input_dirs" not in data
