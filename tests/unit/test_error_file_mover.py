from pathlib import Path

from vbc.pipeline.error_file_mover import collect_error_entries, move_failed_files


def test_move_failed_files_moves_source_and_err(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "input_out"
    errors_dir = tmp_path / "input_err"

    (input_dir / "sub").mkdir(parents=True)
    (output_dir / "sub").mkdir(parents=True)

    source = input_dir / "sub" / "clip.mov"
    source.write_bytes(b"x" * 10)

    err_file = output_dir / "sub" / "clip.err"
    err_file.write_text("Compression failed")

    moved = move_failed_files(
        [input_dir],
        {input_dir: output_dir},
        {input_dir: errors_dir},
        [".mov", ".mp4"],
        logger=None,
    )

    assert len(moved) == 1
    assert not source.exists()
    assert not err_file.exists()
    assert (errors_dir / "sub" / "clip.mov").exists()
    assert (errors_dir / "sub" / "clip.err").exists()


def test_move_failed_files_moves_multiple_sources(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "input_out"
    errors_dir = tmp_path / "input_err"

    input_dir.mkdir()
    output_dir.mkdir()

    source_mp4 = input_dir / "clip.mp4"
    source_avi = input_dir / "clip.avi"
    source_mp4.write_bytes(b"x" * 10)
    source_avi.write_bytes(b"x" * 10)

    err_file = output_dir / "clip.err"
    err_file.write_text("Compression failed")

    moved = move_failed_files(
        [input_dir],
        {input_dir: output_dir},
        {input_dir: errors_dir},
        [".mp4", ".avi"],
        logger=None,
    )

    assert len(moved) == 2
    assert not source_mp4.exists()
    assert not source_avi.exists()
    assert (errors_dir / "clip.mp4").exists()
    assert (errors_dir / "clip.avi").exists()
    assert (errors_dir / "clip.err").exists()


def test_collect_error_entries_counts(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "input_out"
    errors_dir = tmp_path / "input_err"
    output_dir.mkdir(parents=True)

    (output_dir / "a.err").write_text("err")
    (output_dir / "sub").mkdir()
    (output_dir / "sub" / "b.err").write_text("err")

    entries = collect_error_entries(
        [input_dir],
        {input_dir: output_dir},
        {input_dir: errors_dir},
    )

    assert len(entries) == 2


def test_move_failed_files_handles_dotted_names(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "input_out"
    errors_dir = tmp_path / "input_err"

    input_dir.mkdir()
    output_dir.mkdir()

    source = input_dir / "a.b.c.mp4"
    source.write_bytes(b"x" * 10)

    err_file = output_dir / "a.b.c.err"
    err_file.write_text("error")

    moved = move_failed_files(
        [input_dir],
        {input_dir: output_dir},
        {input_dir: errors_dir},
        [".mp4"],
        logger=None,
    )

    assert len(moved) == 1
    assert not source.exists()
    assert not err_file.exists()
    assert (errors_dir / "a.b.c.mp4").exists()
    assert (errors_dir / "a.b.c.err").exists()
