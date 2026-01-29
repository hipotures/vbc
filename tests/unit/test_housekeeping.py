from pathlib import Path

from vbc.infrastructure.housekeeping import HousekeepingService


def test_cleanup_output_markers_removes_when_source_exists(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "input_out"
    errors_dir = tmp_path / "input_err"
    input_dir.mkdir()
    output_dir.mkdir()

    source = input_dir / "video.mp4"
    source.write_bytes(b"x" * 10)

    tmp_marker = output_dir / "video.tmp"
    err_marker = output_dir / "video.err"
    tmp_marker.write_text("partial")
    err_marker.write_text("failed")

    housekeeper = HousekeepingService()
    housekeeper.cleanup_output_markers(
        input_dir=input_dir,
        output_dir=output_dir,
        errors_dir=errors_dir,
        clean_errors=True,
        logger=None,
    )

    assert not tmp_marker.exists()
    assert not err_marker.exists()
    assert not (errors_dir / "video.tmp").exists()
    assert not (errors_dir / "video.err").exists()


def test_cleanup_output_markers_moves_when_source_missing(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "input_out"
    errors_dir = tmp_path / "input_err"
    input_dir.mkdir()
    output_dir.mkdir()

    tmp_marker = output_dir / "orphan.tmp"
    err_marker = output_dir / "orphan.err"
    tmp_marker.write_text("partial")
    err_marker.write_text("failed")

    housekeeper = HousekeepingService()
    housekeeper.cleanup_output_markers(
        input_dir=input_dir,
        output_dir=output_dir,
        errors_dir=errors_dir,
        clean_errors=True,
        logger=None,
    )

    moved_tmp = errors_dir / "orphan.tmp"
    moved_err = errors_dir / "orphan.err"
    assert moved_tmp.exists()
    assert moved_err.exists()
    assert not tmp_marker.exists()
    assert not err_marker.exists()


def test_cleanup_output_markers_keeps_err_when_clean_errors_false(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "input_out"
    errors_dir = tmp_path / "input_err"
    input_dir.mkdir()
    output_dir.mkdir()

    source = input_dir / "video.mp4"
    source.write_bytes(b"x" * 10)

    tmp_marker = output_dir / "video.tmp"
    err_marker = output_dir / "video.err"
    tmp_marker.write_text("partial")
    err_marker.write_text("failed")

    housekeeper = HousekeepingService()
    housekeeper.cleanup_output_markers(
        input_dir=input_dir,
        output_dir=output_dir,
        errors_dir=errors_dir,
        clean_errors=False,
        logger=None,
    )

    assert not tmp_marker.exists()
    assert err_marker.exists()
    assert not (errors_dir / "video.tmp").exists()
    assert not (errors_dir / "video.err").exists()
