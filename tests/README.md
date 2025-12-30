# VBC Test Suite

Comprehensive test suite for VBC (Video Batch Compression) modular architecture.

## Test Statistics

- **Total Tests**: 50
- **Unit Tests**: 40 (tests/unit/)
- **Integration Tests**: 10 (tests/integration/)
- **Pass Rate**: 100%

## Directory Structure

```
tests/
├── README.md                 # This file
├── conftest.py              # Shared fixtures and pytest configuration
├── data/                    # Real test videos (10s clips)
│   ├── sony_10s.mp4
│   ├── gh7_10s.mp4
│   └── dji_10s.mp4
├── unit/                    # Unit tests (40 tests)
│   ├── test_config_models.py       # Pydantic validation tests
│   ├── test_domain_models.py       # Domain entity tests
│   ├── test_event_bus.py           # Event system tests
│   ├── test_file_scanner.py        # File discovery tests
│   ├── test_exif_tool.py           # Metadata extraction tests
│   ├── test_ffprobe.py             # Stream info tests
│   ├── test_ffmpeg.py              # Compression command tests
│   ├── test_housekeeping.py        # Cleanup service tests
│   ├── test_decision_logic.py      # CQ/rotation logic tests
│   ├── test_ui_state.py            # UI state management tests
│   ├── test_ui_manager.py          # Event→UI integration tests
│   ├── test_keyboard.py            # Keyboard event tests
│   └── test_dashboard.py           # Dashboard initialization tests
└── integration/             # Integration tests (10 tests)
    ├── test_orchestrator.py        # Pipeline flow tests
    ├── test_concurrency.py         # Thread management tests
    ├── test_error_markers.py       # Error file handling tests
    ├── test_hw_cap.py              # Hardware capability tests
    ├── test_skipping.py            # Skip logic tests (AV1, min ratio)
    └── test_color_fix.py           # FFmpeg 7.x color space tests
```

## Running Tests

### Run All Tests

```bash
pytest tests/
```

### Run Only Unit Tests

```bash
pytest tests/unit/ -v
```

### Run Only Integration Tests

```bash
pytest tests/integration/ -v
```

### Run With Coverage

```bash
pytest tests/ --cov=vbc --cov-report=html
```

Coverage report will be generated in `htmlcov/index.html`.

### Run Specific Test File

```bash
pytest tests/unit/test_config_models.py -v
```

### Run Tests Matching Pattern

```bash
pytest tests/ -k "ffmpeg" -v
```

## Test Markers

Tests are tagged with custom markers for selective execution:

- `@pytest.mark.unit` - Unit tests (fast, no I/O)
- `@pytest.mark.integration` - Integration tests (with mocks)
- `@pytest.mark.slow` - Slow tests (real files, skipped by default)

### Run Only Fast Tests

```bash
pytest tests/unit/ -m "unit"
```

### Run Integration Tests

```bash
pytest tests/integration/ -m "integration"
```

### Skip Slow Tests

```bash
pytest tests/ -m "not slow"
```

## Test Coverage by Component

### Configuration Layer (5 tests)
- ✅ Pydantic validation (threads, CQ, angles)
- ✅ YAML config loading
- ✅ Default values
- ✅ Invalid parameter rejection

### Domain Layer (7 tests)
- ✅ VideoFile/VideoMetadata models
- ✅ CompressionJob status flow
- ✅ Event creation and structure
- ✅ EventBus pub/sub (3 patterns)

### Infrastructure Layer (15 tests)
- ✅ FileScanner (extensions, min size, ignore _out)
- ✅ ExifTool adapter (metadata extraction, copying)
- ✅ FFprobe adapter (stream info parsing, error handling)
- ✅ FFmpeg adapter (command generation GPU/CPU, rotation, success/failure)
- ✅ Housekeeping service (cleanup .tmp, .err, OSError handling)

### Pipeline Layer (2 tests)
- ✅ Decision logic (dynamic CQ, auto-rotation)
- ✅ Orchestrator sequential flow

### UI Layer (11 tests)
- ✅ UIState (initialization, stats update, active jobs, recent limit)
- ✅ UIManager (event→state integration)
- ✅ Keyboard events (initialization, stop event, event types)
- ✅ Dashboard (initialization, context manager)

### Integration Tests (10 tests)
- ✅ Concurrency management (thread pool, submit-on-demand)
- ✅ Error markers (.err creation, skip, clean_errors retry)
- ✅ Hardware capability detection (NVENC limits)
- ✅ Smart skipping (AV1 codec, min compression ratio)
- ✅ Color space fix (FFmpeg 7.x "reserved" handling)
- ✅ Pipeline orchestration (discovery → metadata → compression → events)

## Fixtures

### Shared Fixtures (conftest.py)

- `sample_config` - Default AppConfig for testing
- `config_with_dynamic_cq` - Config with camera-specific CQ rules
- `config_yaml_path(tmp_path)` - Creates temporary YAML config
- `event_bus` - Fresh EventBus instance
- `test_input_dir(tmp_path)` - Temporary input directory
- `test_output_dir(tmp_path)` - Temporary output directory
- `dummy_video_files(test_input_dir)` - Dummy MP4 files for testing
- `real_test_videos(tmp_path)` - Real 10s video clips with metadata

### Real Test Videos

Located in `tests/data/`:
- `sony_10s.mp4` - Sony ILCE-7RM5 (4K)
- `gh7_10s.mp4` - Panasonic DC-GH7 (with GPS)
- `dji_10s.mp4` - DJI OsmoPocket3 (vertical)

These files are used for integration tests marked with `@pytest.mark.slow`.

## Mocking Strategy

### Unit Tests
- Use `unittest.mock.MagicMock` for all external dependencies
- Mock subprocess calls (ffmpeg, ffprobe, exiftool)
- Mock file I/O where appropriate
- Verify interactions via `assert_called_once()`, `call_args`, etc.

### Integration Tests
- Mock infrastructure adapters but test real orchestrator logic
- Use real temporary directories (`tmp_path` fixture)
- Verify end-to-end flows without actual video processing

### Key Mocking Patterns

**FFprobe Mock (returns valid dict):**
```python
mock_ffprobe = MagicMock()
mock_ffprobe.get_stream_info.return_value = {
    'width': 1920,
    'height': 1080,
    'codec': 'h264',
    'fps': 30.0,
    'color_space': None,  # Must be None or string (not MagicMock!)
    'duration': 10.0
}
```

**FFmpeg Mock (simulates completion):**
```python
def compress_side_effect(job, config, **kwargs):
    job.status = JobStatus.COMPLETED
mock_ffmpeg.compress.side_effect = compress_side_effect
```

**ThreadPoolExecutor Mock (for concurrency tests):**
```python
with patch("concurrent.futures.ThreadPoolExecutor") as MockExecutor, \
     patch("concurrent.futures.wait") as MockWait:

    mock_executor_instance = MagicMock()
    MockExecutor.return_value.__enter__.return_value = mock_executor_instance

    def mock_wait_func(futures_set, timeout=None, return_when=None):
        return (futures_set, set())  # All done, none pending
    MockWait.side_effect = mock_wait_func
```

## Common Issues & Solutions

### Issue: Pydantic ValidationError for color_space

**Symptom:**
```
ValidationError: color_space
  Input should be a valid string [type=string_type, input_value=<MagicMock ...>]
```

**Solution:**
Mock ffprobe to return actual values (not MagicMock):
```python
mock_ffprobe.get_stream_info.return_value = {
    'color_space': None,  # ✅ Use None or "bt709"
    # NOT: 'color_space': MagicMock()  # ❌ Will fail
}
```

### Issue: ValueError: not enough values to unpack

**Symptom:**
```
ValueError: not enough values to unpack (expected 2, got 0)
```

**Solution:**
Mock `concurrent.futures.wait()` to return tuple:
```python
def mock_wait_func(futures_set, timeout=None, return_when=None):
    return (futures_set, set())  # (done, pending)
MockWait.side_effect = mock_wait_func
```

### Issue: AttributeError: 'str' object has no attribute 'with_suffix'

**Symptom:**
```
AttributeError: 'str' object has no attribute 'with_suffix'
```

**Solution:**
Fixed in orchestrator.py:392 by wrapping fallback in `Path()`:
```python
except ValueError:
    rel_path = Path(vf.path.name)  # ✅ Wrap in Path
    # NOT: rel_path = vf.path.name  # ❌ Returns string
```

## Adding New Tests

### Unit Test Template

```python
import pytest
from vbc.your_module import YourClass

def test_your_feature():
    """Test description following Given-When-Then pattern."""
    # Given
    instance = YourClass()

    # When
    result = instance.do_something()

    # Then
    assert result == expected_value
```

### Integration Test Template

```python
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from vbc.pipeline.orchestrator import Orchestrator
from vbc.config.models import AppConfig, GeneralConfig

@pytest.mark.integration
def test_your_integration_scenario(tmp_path):
    """Test end-to-end scenario description."""
    # Setup
    config = AppConfig(general=GeneralConfig(threads=1, debug=False))

    mock_scanner = MagicMock()
    mock_ffprobe = MagicMock()
    mock_ffprobe.get_stream_info.return_value = {
        'width': 1920, 'height': 1080, 'codec': 'h264',
        'fps': 30.0, 'color_space': None, 'duration': 10.0
    }
    # ... setup other mocks

    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=mock_scanner,
        exif_adapter=MagicMock(),
        ffprobe_adapter=mock_ffprobe,
        ffmpeg_adapter=MagicMock()
    )

    # Execute
    orchestrator.run(tmp_path / "input")

    # Verify
    assert expected_behavior
```

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.12'
      - name: Install dependencies
        run: |
          pip install -e ".[dev]"
      - name: Run tests
        run: |
          pytest tests/ -v --cov=vbc --cov-report=xml
      - name: Upload coverage
        uses: codecov/codecov-action@v3
```

## Test Maintenance

### When Adding New Features

1. **Write tests first** (TDD approach preferred)
2. **Add unit tests** for new domain/infrastructure components
3. **Add integration test** if feature spans multiple components
4. **Update this README** if new test categories are added
5. **Ensure 100% pass rate** before committing

### When Refactoring

1. **Run existing tests** to ensure behavior preserved
2. **Update mocks** if interfaces change
3. **Add regression tests** for bugs discovered

## Test Philosophy

This test suite follows **Arrange-Act-Assert (AAA)** pattern and adheres to:

1. **Fast Feedback**: Unit tests run in <1s, integration in <10s
2. **Isolation**: Each test is independent and can run in any order
3. **Clear Intent**: Test names describe what is being tested
4. **No Flakiness**: Deterministic results using mocks and tmp_path
5. **Comprehensive Coverage**: All critical paths tested
6. **Maintainability**: Shared fixtures, clear mocking patterns

---

**Last Updated**: 2025-12-21
**Test Suite Version**: 1.0
**Pass Rate**: 50/50 (100%)
