import pytest
from pathlib import Path
from unittest.mock import MagicMock
from vbc.pipeline.orchestrator import Orchestrator
from vbc.config.models import AppConfig, GeneralConfig
from vbc.ui.keyboard import ThreadControlEvent, RequestShutdown

def test_concurrency_threads_limit():
    """Test that orchestrator respects thread limits via internal condition variable."""
    config = AppConfig(general=GeneralConfig(threads=2))

    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock()
    )

    # Verify thread controller initialized with correct value
    assert orchestrator._current_max_threads == 2

    # Test increasing threads
    old_val = orchestrator._current_max_threads
    with orchestrator._thread_lock:
        orchestrator._current_max_threads = min(16, old_val + 1)
    assert orchestrator._current_max_threads == 3

    # Test decreasing threads
    with orchestrator._thread_lock:
        orchestrator._current_max_threads = max(1, orchestrator._current_max_threads - 1)
    assert orchestrator._current_max_threads == 2

def test_concurrency_dynamic_adjustment():
    """Test that thread count can be adjusted dynamically via events."""
    config = AppConfig(general=GeneralConfig(threads=4))

    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock()
    )

    # Initial state
    assert orchestrator._current_max_threads == 4
    assert orchestrator._active_threads == 0

    # Simulate ThreadControlEvent - increase
    event_increase = ThreadControlEvent(change=1)
    orchestrator._on_thread_control(event_increase)
    assert orchestrator._current_max_threads == 5

    # Simulate ThreadControlEvent - decrease
    event_decrease = ThreadControlEvent(change=-1)
    orchestrator._on_thread_control(event_decrease)
    assert orchestrator._current_max_threads == 4

def test_concurrency_max_limit():
    """Test that threads cannot exceed max limit of 16."""
    config = AppConfig(general=GeneralConfig(threads=15))

    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock()
    )

    assert orchestrator._current_max_threads == 15

    # Try to increase beyond limit
    event_increase = ThreadControlEvent(change=1)
    orchestrator._on_thread_control(event_increase)
    assert orchestrator._current_max_threads == 16  # Should cap at 16

    # Try to increase again - should stay at 16
    orchestrator._on_thread_control(event_increase)
    assert orchestrator._current_max_threads == 16

def test_concurrency_min_limit():
    """Test that threads cannot go below 1."""
    config = AppConfig(general=GeneralConfig(threads=2))

    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        exif_adapter=MagicMock(),
        file_scanner=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock()
    )

    # Decrease to 1
    event_decrease = ThreadControlEvent(change=-1)
    orchestrator._on_thread_control(event_decrease)
    assert orchestrator._current_max_threads == 1

    # Try to decrease below 1 - should stay at 1
    orchestrator._on_thread_control(event_decrease)
    assert orchestrator._current_max_threads == 1

def test_graceful_shutdown_event():
    """Test that shutdown event sets shutdown flag."""
    config = AppConfig(general=GeneralConfig(threads=4))

    orchestrator = Orchestrator(
        config=config,
        event_bus=MagicMock(),
        file_scanner=MagicMock(),
        exif_adapter=MagicMock(),
        ffprobe_adapter=MagicMock(),
        ffmpeg_adapter=MagicMock()
    )

    assert not orchestrator._shutdown_requested

    # Simulate shutdown event
    event = RequestShutdown()
    orchestrator._on_shutdown_request(event)

    assert orchestrator._shutdown_requested
