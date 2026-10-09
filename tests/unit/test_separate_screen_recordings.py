import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import separate_screen_recordings as splitter


CAMERA = {
    "ImageWidth": 3840,
    "ImageHeight": 2160,
    "CompressorID": "hvc1",
    "GPSCoordinates": "50.0923 19.8983",
    "AndroidVersion": 12,
    "Rotation": 90,
}
SCREEN = {
    "ImageWidth": 1080,
    "ImageHeight": 2400,
    "CompressorID": "avc1",
    "AndroidVersion": 12,
    "Rotation": 0,
}


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (CAMERA, "camera"),
        (SCREEN, "screen"),
        ({**CAMERA, "GPSCoordinates": None}, "unknown"),
        ({**SCREEN, "GPSCoordinates": "50 19"}, "unknown"),
        ({**SCREEN, "ImageHeight": 1920}, "unknown"),
        ({**SCREEN, "AndroidVersion": None}, "unknown"),
        ({**SCREEN, "Rotation": 90}, "unknown"),
        ({**SCREEN, "Error": "File format error"}, "unknown"),
        ({**SCREEN, "Warning": "Truncated data"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_observed_profiles_and_ambiguous_metadata(tags, expected):
    assert splitter.classify(tags) == expected


def make_files(root):
    folder = root / "2022" / "20220705"
    folder.mkdir(parents=True)
    camera = folder / "renamed-camera.mp4"
    screen = folder / "renamed-screen.MP4"
    unknown = folder / "unknown.mp4"
    camera.write_bytes(b"camera")
    screen.write_bytes(b"screen")
    unknown.write_bytes(b"unknown")
    (folder / "settings.yaml").write_text("unchanged")
    return camera, screen, unknown


def mock_metadata(monkeypatch):
    monkeypatch.setattr(
        splitter,
        "read_tags",
        lambda paths: {
            path: CAMERA if "camera" in path.name else SCREEN if "screen" in path.name else {}
            for path in paths
        },
    )


def test_dry_run_preserves_all_sources_and_creates_no_output(tmp_path, monkeypatch, capsys):
    root = tmp_path / "QVR"
    camera, screen, unknown = make_files(root)
    output = tmp_path / "screens"
    mock_metadata(monkeypatch)

    assert splitter.main([str(root), "--output", str(output), "--dry-run"]) == 0

    assert camera.read_bytes() == b"camera"
    assert screen.read_bytes() == b"screen"
    assert unknown.read_bytes() == b"unknown"
    assert not output.exists()
    text = capsys.readouterr().out
    assert str(output / "QVR/2022/20220705/renamed-screen.MP4") in text
    assert "camera=1, screen=1, unknown=1" in text


def test_move_preserves_structure_for_multiple_roots(tmp_path, monkeypatch):
    roots = [tmp_path / "QVR", tmp_path / "other"]
    sources = [make_files(root) for root in roots]
    output = tmp_path / "screens"
    mock_metadata(monkeypatch)
    timestamps = [source[1].stat().st_mtime_ns for source in sources]

    assert splitter.main([*(str(root) for root in roots), "--output", str(output)]) == 0

    for root, (camera, screen, unknown), timestamp in zip(roots, sources, timestamps):
        destination = output / root.name / screen.relative_to(root)
        assert destination.read_bytes() == b"screen"
        assert destination.stat().st_mtime_ns == timestamp
        assert not screen.exists()
        assert camera.exists() and unknown.exists()
        assert (camera.parent / "settings.yaml").read_text() == "unchanged"


def test_collision_aborts_entire_plan_before_any_move(tmp_path, monkeypatch):
    root = tmp_path / "QVR"
    _, screen, _ = make_files(root)
    first = root / "aaa-screen.mp4"
    first.write_bytes(b"first")
    output = tmp_path / "screens"
    destination = output / root.name / screen.relative_to(root)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")
    mock_metadata(monkeypatch)

    assert splitter.main([str(root), "--output", str(output)]) == 1

    assert first.exists() and screen.exists()
    assert destination.read_bytes() == b"existing"
    assert not (output / root.name / first.name).exists()


@pytest.mark.parametrize("layout", ["nested-output", "ancestor-output", "nested-input", "same-name"])
def test_rejects_overlapping_or_ambiguous_roots(tmp_path, layout):
    root = tmp_path / "QVR"
    root.mkdir()
    output = tmp_path / "screens"
    roots = [root]
    if layout == "nested-output":
        output = root / "screens"
    elif layout == "ancestor-output":
        output = tmp_path
    elif layout == "nested-input":
        nested = root / "nested"
        nested.mkdir()
        roots.append(nested)
    else:
        duplicate = tmp_path / "other" / root.name
        duplicate.mkdir(parents=True)
        roots.append(duplicate)

    with pytest.raises(ValueError):
        splitter.build_plan(roots, output)


def test_skips_symlinked_files_and_directories(tmp_path, monkeypatch):
    root = tmp_path / "QVR"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    video = outside / "screen.mp4"
    video.write_bytes(b"outside")
    (root / "linked.mp4").symlink_to(video)
    (root / "linked-directory").symlink_to(outside, target_is_directory=True)
    mock_metadata(monkeypatch)

    assert splitter.build_plan([root], tmp_path / "screens") == []
    assert video.read_bytes() == b"outside"


def test_rejects_symlinked_destination_subdirectory(tmp_path, monkeypatch):
    root = tmp_path / "QVR"
    make_files(root)
    output = tmp_path / "screens"
    output.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (output / root.name).symlink_to(other, target_is_directory=True)
    mock_metadata(monkeypatch)

    with pytest.raises(ValueError, match="Invalid destination directory"):
        splitter.build_plan([root], output)


def test_cross_filesystem_move_and_exclusive_destination(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original video")
    timestamp = source.stat().st_mtime_ns
    destination = tmp_path / "output/video.mp4"

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(splitter.os, "link", cross_device)
    splitter.move_file(source, destination)

    assert destination.read_bytes() == b"original video"
    assert destination.stat().st_mtime_ns == timestamp
    assert not source.exists()
    source.write_bytes(b"second source")
    with pytest.raises(FileExistsError):
        splitter.move_file(source, destination)
    assert source.read_bytes() == b"second source"
    assert destination.read_bytes() == b"original video"


def test_failed_cross_filesystem_copy_keeps_source_and_removes_partial_destination(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original video")
    destination = tmp_path / "output/video.mp4"

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "Cross-device link")

    def failed_copy(original, target):
        target.write(b"partial")
        raise OSError("Disk full")

    monkeypatch.setattr(splitter.os, "link", cross_device)
    monkeypatch.setattr(splitter.shutil, "copyfileobj", failed_copy)

    with pytest.raises(OSError, match="Disk full"):
        splitter.move_file(source, destination)
    assert source.read_bytes() == b"original video"
    assert not destination.exists()


@pytest.mark.parametrize("response", ["[]", "{}", "not-json"])
def test_metadata_scan_rejects_missing_or_invalid_results(tmp_path, monkeypatch, response):
    monkeypatch.setattr(
        splitter.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=response, stderr=""),
    )
    with pytest.raises(ValueError):
        splitter.read_tags([tmp_path / "screen.mp4"])


def test_metadata_scan_batches_and_passes_paths_as_arguments(tmp_path, monkeypatch):
    paths = [tmp_path / f"screen {index}.mp4" for index in range(201)]
    calls = []

    def scan(command, **kwargs):
        calls.append(command)
        batch = [argument for argument in command if argument.endswith(".mp4")]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"SourceFile": path, **SCREEN} for path in batch]),
            stderr="",
        )

    monkeypatch.setattr(splitter.subprocess, "run", scan)

    assert set(splitter.read_tags(paths)) == set(paths)
    assert len(calls) == 2
    assert str(paths[0]) in calls[0]
