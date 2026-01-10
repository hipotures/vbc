import pytest
import subprocess
import shutil
import yaml
from pathlib import Path
from vbc.config.models import AppConfig
from vbc.infrastructure.event_bus import EventBus

# ============================================================================
# Configuration Fixtures
# ============================================================================

@pytest.fixture
def sample_config():
    """Returns a sample AppConfig object for testing."""
    return AppConfig(
        general={
            "threads": 4,
            "gpu": True,
            "copy_metadata": True,
            "use_exif": True,
            "extensions": [".mp4", ".mov", ".avi"],
            "min_size_bytes": 1024,
            "filter_cameras": [],
            "dynamic_cq": {},
            "prefetch_factor": 1,
            "clean_errors": False,
            "skip_av1": False,
            "strip_unicode_display": True,
            "manual_rotation": None,
            "min_compression_ratio": 0.1,
            "debug": False,
        },
        autorotate={
            "patterns": {}
        }
    )

@pytest.fixture
def config_with_dynamic_cq():
    """Returns config with dynamic CQ rules."""
    return AppConfig(
        general={
            "threads": 2,
            "gpu": False,
            "copy_metadata": True,
            "use_exif": True,
            "extensions": [".mp4"],
            "min_size_bytes": 0,
            "filter_cameras": [],
            "dynamic_cq": {
                "DC-GH7": 30,
                "ILCE-7RM5": 35,
                "DJI OsmoPocket3": 35
            },
            "prefetch_factor": 1,
            "clean_errors": False,
            "skip_av1": False,
            "strip_unicode_display": True,
            "manual_rotation": None,
            "min_compression_ratio": 0.1,
            "debug": False,
        },
        autorotate={
            "patterns": {
                "QVR_\\d{8}_\\d{6}\\.mp4": 180
            }
        }
    )

@pytest.fixture
def config_yaml_path(tmp_path):
    """Creates a temporary YAML config file."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    conf_file = conf_dir / "vbc.yaml"

    content = {
        'general': {
            'threads': 2,
            'gpu': False,
            'copy_metadata': True,
            'use_exif': True,
            'extensions': ['mp4', 'mov'],
            'min_size_bytes': 0,
            'filter_cameras': [],
            'dynamic_cq': {
                'DC-GH7': 30,
                'ILCE-7RM5': 35,
            },
            'prefetch_factor': 1,
            'clean_errors': False,
            'skip_av1': False,
            'strip_unicode_display': True,
            'manual_rotation': None,
            'min_compression_ratio': 0.1,
            'debug': False,
        },
        'autorotate': {
            'patterns': {
                'QVR_\\d{8}_\\d{6}\\.mp4': 180
            }
        }
    }

    with open(conf_file, 'w') as f:
        yaml.dump(content, f)

    return conf_file

# ============================================================================
# EventBus Fixtures
# ============================================================================

@pytest.fixture
def event_bus():
    """Returns a fresh EventBus instance."""
    return EventBus()

# ============================================================================
# File System Fixtures
# ============================================================================

@pytest.fixture
def test_input_dir(tmp_path):
    """Creates a test input directory."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    return input_dir

@pytest.fixture
def test_output_dir(tmp_path):
    """Creates a test output directory."""
    output_dir = tmp_path / "input_out"
    output_dir.mkdir()
    return output_dir

@pytest.fixture
def dummy_video_files(test_input_dir):
    """Creates dummy video files in test input directory."""
    files = []

    # Create some dummy MP4 files
    for i in range(3):
        f = test_input_dir / f"video{i}.mp4"
        f.write_bytes(b"dummy video content " * 100)  # ~2KB
        files.append(f)

    # Create a subdirectory with a file
    subdir = test_input_dir / "subdir"
    subdir.mkdir()
    f = subdir / "subvideo.mp4"
    f.write_bytes(b"dummy video content " * 100)
    files.append(f)

    return files

# ============================================================================
# Real Video Fixtures (for integration tests)
# ============================================================================

@pytest.fixture
def real_test_videos(tmp_path):
    """
    Copies real test videos from tests/data/ to temporary directory.
    Uses actual 10s clips from different cameras.
    """
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    data_src = Path(__file__).resolve().parent / "data"

    files_map = {
        "sony_test.mp4": data_src / "sony_10s.mp4",
        "gh7_test.mp4": data_src / "gh7_10s.mp4",
        "dji_test.mp4": data_src / "dji_10s.mp4"
    }

    copied_files = []
    for target_name, src_path in files_map.items():
        if src_path.exists():
            dest = input_dir / target_name
            shutil.copy(src_path, dest)
            copied_files.append(dest)
        else:
            pytest.skip(f"Missing test video: {src_path}. Run integration tests with real data.")

    # Set stable metadata for testing
    metadata_updates = {
        "sony_test.mp4": [
            "-Model=ILCE-7RM5",
            "-Make=Sony",
        ],
        "gh7_test.mp4": [
            "-Model=DC-GH7",
            "-Make=Panasonic",
            "-GPSLatitude=50.0615",
            "-GPSLongitude=19.9380",
        ],
        "dji_test.mp4": [
            "-Model=DJI OsmoPocket3",
            "-Make=DJI",
        ],
    }

    for filename, tags in metadata_updates.items():
        file_path = input_dir / filename
        if file_path.exists():
            subprocess.run(
                ["exiftool", *tags, "-overwrite_original", str(file_path)],
                capture_output=True
            )

    # Add QVR file for rotation testing
    if (data_src / "dji_10s.mp4").exists():
        qvr_file = input_dir / "QVR_20250101_120000.mp4"
        shutil.copy(data_src / "dji_10s.mp4", qvr_file)
        copied_files.append(qvr_file)

    return input_dir, copied_files


def pytest_collection_modifyitems(config, items):
    real_file_prefix = "tests/integration/test_real_files"
    real_items = []
    other_items = []
    for item in items:
        item_path = str(getattr(item, "fspath", getattr(item, "path", "")))
        if real_file_prefix in item_path:
            real_items.append(item)
        else:
            other_items.append(item)
    items[:] = other_items + real_items

# ============================================================================
# Marker for slow tests (integration tests with real files)
# ============================================================================

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (integration tests with real files)"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "unit: marks tests as unit tests"
    )
