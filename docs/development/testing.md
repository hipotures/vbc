# Testing Guide

VBC uses `pytest` for testing. This guide covers writing and running tests.

## Test Structure

```
tests/
├── unit/               # Unit tests (fast, isolated)
│   ├── test_config.py
│   ├── test_models.py
│   ├── test_events.py
│   └── test_adapters.py
├── integration/        # Integration tests (slower, real dependencies)
│   ├── test_pipeline.py
│   └── test_orchestrator.py
└── fixtures/           # Test data
    ├── sample.mp4
    └── corrupted.mp4
```

## Running Tests

### All Tests

```bash
uv run pytest
```

### Specific Test File

```bash
uv run pytest tests/unit/test_config.py
```

### Specific Test Function

```bash
uv run pytest tests/unit/test_config.py::test_load_config
```

### With Coverage

```bash
# Terminal report
uv run pytest --cov=vbc

# HTML report
uv run pytest --cov=vbc --cov-report=html
# Open htmlcov/index.html
```

### Watch Mode

```bash
uv run pytest-watch
```

### Verbose Mode

```bash
uv run pytest -v
```

### Stop on First Failure

```bash
uv run pytest -x
```

## Writing Tests

### Unit Test Example

```python
# tests/unit/test_config.py
import pytest
from pathlib import Path
from pydantic import ValidationError
from vbc.config.models import GeneralConfig, AutoRotateConfig, AppConfig
from vbc.infrastructure.ffmpeg import extract_quality_value, replace_quality_value

def test_general_config_defaults():
    """Test GeneralConfig with default values."""
    config = GeneralConfig()
    assert config.threads == 1
    assert config.gpu is True
    assert config.copy_metadata is True

def test_encoder_defaults():
    """Test default encoder quality values."""
    config = AppConfig()
    assert extract_quality_value(config.gpu_encoder.common_args) == 45
    assert extract_quality_value(config.cpu_encoder.common_args) == 32

def test_general_config_validation():
    """Test GeneralConfig validation rules."""
    # Valid config
    config = GeneralConfig(threads=8)
    assert config.threads == 8

    # Invalid: threads must be > 0
    with pytest.raises(ValidationError) as exc_info:
        GeneralConfig(threads=0)
    assert "greater than 0" in str(exc_info.value)

    # Invalid: min_compression_ratio must be 0.0-1.0
    with pytest.raises(ValidationError) as exc_info:
        GeneralConfig(min_compression_ratio=1.5)
    assert "less than or equal to 1.0" in str(exc_info.value)

def test_autorotate_validation():
    """Test AutoRotateConfig angle validation."""
    # Valid angles
    config = AutoRotateConfig(patterns={
        "pattern1": 0,
        "pattern2": 90,
        "pattern3": 180,
        "pattern4": 270
    })
    assert config.patterns["pattern3"] == 180

    # Invalid angle
    with pytest.raises(ValidationError) as exc_info:
        AutoRotateConfig(patterns={"pattern": 45})
    assert "Invalid rotation angle" in str(exc_info.value)
```

### Mock Example

```python
# tests/unit/test_adapters.py
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
from vbc.infrastructure.ffprobe import FFprobeAdapter

def test_ffprobe_adapter():
    """Test FFprobeAdapter with mocked subprocess."""
    adapter = FFprobeAdapter()

    # Mock subprocess.run
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = """codec_name=h264
width=1920
height=1080
avg_frame_rate=60/1
"""

    with patch('subprocess.run', return_value=mock_result):
        info = adapter.get_stream_info(Path("test.mp4"))

    assert info['codec'] == 'h264'
    assert info['width'] == 1920
    assert info['height'] == 1080
    assert info['fps'] == 60.0
```

### Event Bus Testing

```python
# tests/unit/test_events.py
from vbc.infrastructure.event_bus import EventBus
from vbc.domain.events import JobStarted, JobCompleted
from vbc.domain.models import CompressionJob, VideoFile, JobStatus
from pathlib import Path

def test_event_bus_subscription():
    """Test EventBus publish/subscribe."""
    bus = EventBus()
    received_events = []

    def handler(event: JobStarted):
        received_events.append(event)

    bus.subscribe(JobStarted, handler)

    # Publish event
    job = CompressionJob(
        source_file=VideoFile(path=Path("test.mp4"), size_bytes=1000)
    )
    event = JobStarted(job=job)
    bus.publish(event)

    assert len(received_events) == 1
    assert received_events[0].job.source_file.path == Path("test.mp4")

def test_event_bus_multiple_subscribers():
    """Test multiple subscribers to same event."""
    bus = EventBus()
    calls = []

    def handler1(event: JobCompleted):
        calls.append(('handler1', event))

    def handler2(event: JobCompleted):
        calls.append(('handler2', event))

    bus.subscribe(JobCompleted, handler1)
    bus.subscribe(JobCompleted, handler2)

    job = CompressionJob(
        source_file=VideoFile(path=Path("test.mp4"), size_bytes=1000)
    )
    bus.publish(JobCompleted(job=job))

    assert len(calls) == 2
    assert calls[0][0] == 'handler1'
    assert calls[1][0] == 'handler2'
```

### Integration Test Example

```python
# tests/integration/test_orchestrator.py
import pytest
from pathlib import Path
from vbc.config.loader import load_config
from vbc.infrastructure.event_bus import EventBus
from vbc.infrastructure.file_scanner import FileScanner
from vbc.infrastructure.exif_tool import ExifToolAdapter
from vbc.infrastructure.ffprobe import FFprobeAdapter
from vbc.infrastructure.ffmpeg import FFmpegAdapter
from vbc.pipeline.orchestrator import Orchestrator

@pytest.fixture
def test_dir(tmp_path):
    """Create temporary test directory with sample video."""
    test_video = tmp_path / "test.mp4"
    # Create minimal valid MP4 (you'd use a real sample in practice)
    test_video.write_bytes(b"fake video data")
    return tmp_path

@pytest.mark.integration
def test_orchestrator_discovery(test_dir):
    """Test Orchestrator discovery phase."""
    config = load_config()
    config.general.extensions = [".mp4"]
    config.general.min_size_bytes = 0

    bus = EventBus()
    scanner = FileScanner(
        extensions=config.general.extensions,
        min_size_bytes=config.general.min_size_bytes
    )

    orchestrator = Orchestrator(
        config=config,
        event_bus=bus,
        file_scanner=scanner,
        exif_adapter=Mock(),
        ffprobe_adapter=Mock(),
        ffmpeg_adapter=Mock()
    )

    files, stats = orchestrator._perform_discovery(test_dir)

    assert len(files) == 1
    assert files[0].path.name == "test.mp4"
    assert stats['files_found'] >= 1
```

## Test Fixtures

### Shared Fixtures

```python
# tests/conftest.py
import pytest
from pathlib import Path
from vbc.config.models import AppConfig, GeneralConfig
from vbc.infrastructure.event_bus import EventBus

@pytest.fixture
def event_bus():
    """Create fresh EventBus for each test."""
    return EventBus()

@pytest.fixture
def default_config():
    """Create default AppConfig."""
    return AppConfig(general=GeneralConfig())

@pytest.fixture
def sample_video_path():
    """Path to sample test video."""
    return Path(__file__).parent / "fixtures" / "sample.mp4"
```

### Using Fixtures

```python
def test_with_fixtures(event_bus, default_config):
    """Test using fixtures."""
    assert default_config.general.threads == 4
    assert isinstance(event_bus, EventBus)
```

## Mocking External Dependencies

### Mock FFmpeg

```python
from unittest.mock import patch, Mock

def test_ffmpeg_compression():
    """Test FFmpegAdapter with mocked subprocess."""
    with patch('subprocess.Popen') as mock_popen:
        mock_process = Mock()
        mock_process.returncode = 0
        mock_process.stdout = []
        mock_popen.return_value = mock_process

        # Test compression
        adapter = FFmpegAdapter(event_bus=EventBus())
        # ... test logic
```

### Mock ExifTool

```python
def test_exiftool_extraction():
    """Test ExifToolAdapter with mocked exiftool."""
    with patch('exiftool.ExifTool') as mock_et:
        mock_instance = Mock()
        mock_instance.execute_json.return_value = [{
            'QuickTime:ImageWidth': 1920,
            'QuickTime:ImageHeight': 1080,
            'EXIF:Model': 'ILCE-7RM5'
        }]
        mock_et.return_value = mock_instance

        # Test extraction
        adapter = ExifToolAdapter()
        # ... test logic
```

## Test Markers

Use markers to categorize tests:

```python
import pytest

@pytest.mark.unit
def test_fast_unit():
    """Fast unit test."""
    pass

@pytest.mark.integration
def test_slow_integration():
    """Slow integration test."""
    pass

@pytest.mark.slow
def test_very_slow():
    """Very slow test (real video encoding)."""
    pass
```

Run specific markers:

```bash
# Only unit tests
uv run pytest -m unit

# Skip slow tests
uv run pytest -m "not slow"
```

## Parametrized Tests

Test multiple scenarios:

```python
@pytest.mark.parametrize("quality,expected_quality", [
    (35, "high"),
    (45, "medium"),
    (55, "low"),
])
def test_quality_mapping(quality, expected_quality):
    """Test quality-to-label mapping."""
    config = AppConfig()
    args = replace_quality_value(config.gpu_encoder.common_args, quality)
    assert extract_quality_value(args) == quality
    # Assert quality label based on value
```

## Test Coverage Goals

Target coverage levels:

| Module | Target | Current |
|--------|--------|---------|
| `config/` | 95%+ | TBD |
| `domain/` | 95%+ | TBD |
| `infrastructure/` | 80%+ | TBD |
| `pipeline/` | 90%+ | TBD |
| `ui/` | 70%+ | TBD |

Run coverage report:

```bash
uv run pytest --cov=vbc --cov-report=term-missing
```

## Continuous Integration

GitHub Actions runs tests on every PR:

```yaml
# .github/workflows/test.yml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Install uv
        run: curl -LsSf https://astral.sh/uv/install.sh | sh
      - name: Install dependencies
        run: uv sync
      - name: Run tests
        run: uv run pytest --cov=vbc
```

## Debugging Tests

### Print Debugging

```python
def test_with_debug():
    """Test with debug output."""
    result = some_function()
    print(f"Result: {result}")  # Visible with pytest -s
    assert result == expected
```

Run with output:

```bash
uv run pytest -s  # Show print statements
```

### PDB Debugging

```python
def test_with_pdb():
    """Test with debugger."""
    import pdb; pdb.set_trace()  # Breakpoint
    result = some_function()
    assert result == expected
```

Run with debugger:

```bash
uv run pytest --pdb  # Drop into debugger on failure
```

## Best Practices

1. **One assertion per test** (when possible)
2. **Use descriptive test names** (test_what_when_then)
3. **Arrange-Act-Assert** pattern
4. **Mock external dependencies** (filesystem, network, processes)
5. **Use fixtures** for shared setup
6. **Parametrize** for multiple scenarios
7. **Test edge cases** (empty lists, None values, errors)
8. **Test error paths** (not just happy path)

## Example Test Suite

```python
# tests/unit/test_file_scanner.py
import pytest
from pathlib import Path
from vbc.infrastructure.file_scanner import FileScanner

@pytest.fixture
def scanner():
    return FileScanner(
        extensions=[".mp4", ".mov"],
        min_size_bytes=1024
    )

def test_scanner_finds_matching_files(tmp_path, scanner):
    """Test scanner finds files with matching extensions."""
    # Arrange
    video1 = tmp_path / "video1.mp4"
    video2 = tmp_path / "video2.mov"
    video3 = tmp_path / "video3.avi"  # Wrong extension
    video1.write_bytes(b"x" * 2000)
    video2.write_bytes(b"x" * 2000)
    video3.write_bytes(b"x" * 2000)

    # Act
    files = list(scanner.scan(tmp_path))

    # Assert
    assert len(files) == 2
    assert any(f.path.name == "video1.mp4" for f in files)
    assert any(f.path.name == "video2.mov" for f in files)
    assert not any(f.path.name == "video3.avi" for f in files)

def test_scanner_filters_small_files(tmp_path, scanner):
    """Test scanner filters files below minimum size."""
    # Arrange
    small = tmp_path / "small.mp4"
    large = tmp_path / "large.mp4"
    small.write_bytes(b"x" * 500)  # Below 1024 bytes
    large.write_bytes(b"x" * 2000)  # Above 1024 bytes

    # Act
    files = list(scanner.scan(tmp_path))

    # Assert
    assert len(files) == 1
    assert files[0].path.name == "large.mp4"

def test_scanner_skips_output_directories(tmp_path, scanner):
    """Test scanner skips directories ending in _out."""
    # Arrange
    (tmp_path / "normal").mkdir()
    (tmp_path / "normal" / "video.mp4").write_bytes(b"x" * 2000)
    (tmp_path / "videos_out").mkdir()
    (tmp_path / "videos_out" / "output.mp4").write_bytes(b"x" * 2000)

    # Act
    files = list(scanner.scan(tmp_path))

    # Assert
    assert len(files) == 1
    assert files[0].path.parent.name == "normal"
```

## Next Steps

- [Contributing](contributing.md) - Contribution guidelines
