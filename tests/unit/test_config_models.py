import pytest
from pydantic import ValidationError
from vbc.config.models import AppConfig, GeneralConfig, AutoRotateConfig

def test_valid_config():
    data = {
        "general": {
            "threads": 4,
            "cq": 45,
            "gpu": True,
            "copy_metadata": True,
            "use_exif": True,
            "filter_cameras": ["Sony"],
            "dynamic_cq": {"Sony": 35},
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
        GeneralConfig(threads=0, cq=45, gpu=True, copy_metadata=True, use_exif=True, extensions=[".mp4"], min_size_bytes=0)

def test_invalid_cq():
    with pytest.raises(ValidationError):
        GeneralConfig(threads=4, cq=64, gpu=True, copy_metadata=True, use_exif=True, extensions=[".mp4"], min_size_bytes=0)

def test_config_defaults():
    gen = GeneralConfig(threads=1, extensions=[".mp4"])
    assert gen.filter_cameras == []
    assert gen.dynamic_cq == {}
    assert gen.cq == 45
    assert gen.min_compression_ratio == 0.1
    assert gen.queue_sort == "name"
    assert gen.log_path == "/tmp/vbc/compression.log"


def test_queue_sort_alias_size():
    gen = GeneralConfig(threads=1, extensions=[".mp4"], queue_sort="size")
    assert gen.queue_sort == "size-asc"


def test_queue_sort_invalid_mode():
    with pytest.raises(ValidationError):
        GeneralConfig(threads=1, extensions=[".mp4"], queue_sort="bad")


def test_queue_sort_ext_requires_extensions():
    with pytest.raises(ValidationError):
        GeneralConfig(threads=1, extensions=[], queue_sort="ext")

def test_load_config(tmp_path):
    d = tmp_path / "conf"
    d.mkdir()
    f = d / "vbc.yaml"
    f.write_text("""
general:
  threads: 8
  cq: 30
autorotate:
  patterns:
    "test.*": 90
""")
    from vbc.config.loader import load_config
    config = load_config(f)
    assert config.general.threads == 8
    assert config.general.cq == 30
    assert config.autorotate.patterns["test.*"] == 90
