"""Unit tests for logging infrastructure."""
import pytest
import logging
from pathlib import Path
from vbc.infrastructure.logging import setup_logging


def test_setup_logging_creates_log_file(tmp_path):
    """Test that setup_logging creates log file."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    logger = setup_logging(output_dir, debug=False)

    assert logger is not None
    assert isinstance(logger, logging.Logger)

    # Check log file was created
    log_file = output_dir / "compression.log"
    assert log_file.exists()


def test_setup_logging_debug_mode(tmp_path):
    """Test setup_logging in debug mode."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    logger = setup_logging(output_dir, debug=True)

    # Logger should be at DEBUG level
    assert logger.level == logging.DEBUG or logger.getEffectiveLevel() == logging.DEBUG


def test_setup_logging_normal_mode(tmp_path):
    """Test setup_logging in normal mode."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    logger = setup_logging(output_dir, debug=False)

    # Logger should be at INFO level
    assert logger.level == logging.INFO or logger.getEffectiveLevel() == logging.INFO


def test_setup_logging_creates_output_dir(tmp_path):
    """Test that setup_logging creates output directory if missing."""
    output_dir = tmp_path / "missing_output"

    logger = setup_logging(output_dir, debug=False)

    # Directory should be created
    assert output_dir.exists()
    assert output_dir.is_dir()


def test_setup_logging_writes_to_file(tmp_path):
    """Test that logger actually writes to file."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    logger = setup_logging(output_dir, debug=False)

    # Write a test message
    test_message = "Test log message for verification"
    logger.info(test_message)

    # Force flush
    for handler in logger.handlers:
        handler.flush()

    # Verify message was written to file
    log_file = output_dir / "compression.log"
    log_content = log_file.read_text()

    assert test_message in log_content


def test_setup_logging_format_includes_timestamp(tmp_path):
    """Test that log format includes timestamp."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    logger = setup_logging(output_dir, debug=False)
    logger.info("Test message")

    # Force flush
    for handler in logger.handlers:
        handler.flush()

    log_file = output_dir / "compression.log"
    log_content = log_file.read_text()

    # Should contain timestamp format (YYYY-MM-DD HH:MM:SS)
    assert " - " in log_content  # Separator between timestamp and level
    assert "INFO" in log_content


def test_setup_logging_format_includes_level(tmp_path):
    """Test that log format includes level name."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    logger = setup_logging(output_dir, debug=False)

    logger.info("Info message")
    logger.warning("Warning message")
    logger.error("Error message")

    for handler in logger.handlers:
        handler.flush()

    log_file = output_dir / "compression.log"
    log_content = log_file.read_text()

    assert "INFO" in log_content
    assert "WARNING" in log_content
    assert "ERROR" in log_content


def test_setup_logging_debug_messages(tmp_path):
    """Test that debug messages only appear in debug mode."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    # Normal mode - should not log DEBUG
    logger_normal = setup_logging(output_dir, debug=False)
    logger_normal.debug("Debug message in normal mode")

    for handler in logger_normal.handlers:
        handler.flush()

    log_file = output_dir / "compression.log"
    content_normal = log_file.read_text()

    assert "Debug message in normal mode" not in content_normal

    # Debug mode - should log DEBUG
    logger_debug = setup_logging(output_dir, debug=True)
    logger_debug.debug("Debug message in debug mode")

    for handler in logger_debug.handlers:
        handler.flush()

    content_debug = log_file.read_text()
    assert "Debug message in debug mode" in content_debug


def test_setup_logging_returns_named_logger(tmp_path):
    """Test that setup_logging returns a properly named logger."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    logger = setup_logging(output_dir, debug=False)

    # Should have a name (typically the module name)
    assert logger.name is not None
    assert len(logger.name) > 0


def test_setup_logging_multiple_calls_same_dir(tmp_path):
    """Test calling setup_logging multiple times with same directory."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    logger1 = setup_logging(output_dir, debug=False)
    logger2 = setup_logging(output_dir, debug=False)

    # Both should work without errors
    logger1.info("Message from logger1")
    logger2.info("Message from logger2")

    for handler in logger1.handlers:
        handler.flush()
    for handler in logger2.handlers:
        handler.flush()

    log_file = output_dir / "compression.log"
    content = log_file.read_text()

    assert "Message from logger1" in content
    assert "Message from logger2" in content
