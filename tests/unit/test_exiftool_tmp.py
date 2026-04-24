from vbc.infrastructure.exiftool_tmp import (
    cleanup_exiftool_tmp_files,
    exiftool_tmp_path,
    remove_exiftool_tmp_for_target,
)


def test_cleanup_exiftool_tmp_files_removes_stale_files(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    stale = output_dir / "video.mp4_exiftool_tmp"
    normal = output_dir / "video.mp4"
    nested = output_dir / "nested"
    nested.mkdir()
    nested_stale = nested / "clip.mkv_exiftool_tmp"
    stale.write_text("stale")
    normal.write_text("normal")
    nested_stale.write_text("nested stale")

    removed = cleanup_exiftool_tmp_files([output_dir])

    assert sorted(path.name for path in removed) == [
        "clip.mkv_exiftool_tmp",
        "video.mp4_exiftool_tmp",
    ]
    assert not stale.exists()
    assert not nested_stale.exists()
    assert normal.exists()


def test_remove_exiftool_tmp_for_target_removes_only_exact_sibling(tmp_path):
    target = tmp_path / "video.mp4"
    target.write_text("target")
    exact_tmp = exiftool_tmp_path(target)
    exact_tmp.write_text("stale")
    similarly_named = tmp_path / "video.mp4_exiftool_tmp_extra"
    similarly_named.write_text("keep")

    removed = remove_exiftool_tmp_for_target(target)

    assert removed == exact_tmp
    assert target.exists()
    assert not exact_tmp.exists()
    assert similarly_named.exists()


def test_remove_exiftool_tmp_for_target_does_not_remove_directories(tmp_path):
    target = tmp_path / "video.mp4"
    target.write_text("target")
    exact_tmp_dir = exiftool_tmp_path(target)
    exact_tmp_dir.mkdir()

    removed = remove_exiftool_tmp_for_target(target)

    assert removed is None
    assert exact_tmp_dir.is_dir()


def test_cleanup_exiftool_tmp_files_does_not_remove_directories(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    tmp_dir = output_dir / "video.mp4_exiftool_tmp"
    tmp_dir.mkdir()

    removed = cleanup_exiftool_tmp_files([output_dir])

    assert removed == []
    assert tmp_dir.is_dir()
