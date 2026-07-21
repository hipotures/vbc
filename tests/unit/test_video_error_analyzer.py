import json
from argparse import Namespace
from io import StringIO

import pytest
from rich.console import Console

from scripts import video_error_analyzer as analyzer
from scripts.repair_failed_manifests import RepairOutcome


def _entry(error_dir, name, error_text, *, with_manifest=True):
    error_path = error_dir / f"{name}.err"
    error_path.write_text(error_text)
    manifest_path = error_path.with_suffix(".json")
    if with_manifest:
        manifest_path.write_text(json.dumps({"inputs": ["/source/video.mp4"]}))
    return error_path, manifest_path


def _args(**overrides):
    values = {
        "config": None,
        "dry_run": False,
        "repair_ffmpeg_244": False,
        "delete_orphans": False,
        "delete_moov_missing": False,
        "delete_missing_input": False,
        "delete_invalid_dimensions": False,
        "delete_no_video": False,
        "delete_invalid_bitstream": False,
        "delete_hardware_capability": False,
        "delete_ffmpeg_abort": False,
        "delete_ffmpeg_segfault": False,
        "delete_ffmpeg_234": False,
        "delete_unknown": False,
    }
    values.update(overrides)
    return Namespace(**values)


@pytest.mark.parametrize(
    ("error_text", "category"),
    [
        ("ffmpeg exited with code 244", "ffmpeg-244"),
        ("Error number: -12", "ffmpeg-244"),
        ("moov atom not found", "moov-missing"),
        ("Missing manifest input: /video.mp4", "missing-input"),
        ("Invalid video dimensions for /video.mp4: 0x0", "invalid-dimensions"),
        ("input has no video packets", "no-video"),
        ("missing picture in access unit", "invalid-bitstream"),
        ("Hardware is lacking required capabilities", "hardware-capability"),
        ("ffmpeg exited with code -6", "ffmpeg-abort"),
        ("ffmpeg exited with code -11", "ffmpeg-segfault"),
        ("ffmpeg exited with code 234", "ffmpeg-234"),
        ("something new", "unknown"),
    ],
)
def test_classifies_known_errors(error_text, category):
    assert analyzer.classify_error(error_text).key == category


def test_directory_analysis_is_read_only_by_default(tmp_path):
    error_dir = tmp_path / "metadata_err"
    error_dir.mkdir()
    error_path, manifest_path = _entry(
        error_dir,
        "broken",
        "moov atom not found",
    )

    entries = analyzer.collect_error_entries(error_dir)
    outcomes = analyzer.analyze_entries(
        entries,
        _args(),
        Console(file=StringIO()),
    )

    assert outcomes[0].status == "ANALYZED"
    assert error_path.exists()
    assert manifest_path.exists()


def test_selected_delete_removes_only_matching_metadata_pair(tmp_path):
    error_dir = tmp_path / "metadata_err"
    error_dir.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    selected_err, selected_json = _entry(
        error_dir,
        "selected",
        "moov atom not found",
    )
    kept_err, kept_json = _entry(
        error_dir,
        "kept",
        "ffmpeg exited with code -6",
    )

    outcomes = analyzer.analyze_entries(
        analyzer.collect_error_entries(error_dir),
        _args(delete_moov_missing=True),
        Console(file=StringIO()),
    )

    assert {outcome.status for outcome in outcomes} == {"DELETED", "KEPT"}
    assert not selected_err.exists()
    assert not selected_json.exists()
    assert kept_err.exists()
    assert kept_json.exists()
    assert source.exists()


def test_orphan_delete_and_dry_run(tmp_path):
    error_dir = tmp_path / "metadata_err"
    error_dir.mkdir()
    error_path, manifest_path = _entry(
        error_dir,
        "orphan",
        "ffmpeg exited with code 244",
        with_manifest=False,
    )

    entries = analyzer.collect_error_entries(error_dir)
    outcomes = analyzer.analyze_entries(
        entries,
        _args(delete_orphans=True, dry_run=True),
        Console(file=StringIO()),
    )

    assert entries[0].category is analyzer.ORPHAN
    assert outcomes[0].status == "WOULD DELETE"
    assert error_path.exists()
    assert not manifest_path.exists()


def test_repair_flag_delegates_to_existing_repair(tmp_path, monkeypatch):
    error_dir = tmp_path / "metadata_err"
    error_dir.mkdir()
    error_path, manifest_path = _entry(
        error_dir,
        "repairable",
        "ffmpeg exited with code 244",
    )
    called = {}

    def fake_repair(candidate, config, console, *, dry_run):
        called.update(candidate=candidate, config=config, dry_run=dry_run)
        return RepairOutcome(manifest_path, "READY", "repair plan")

    monkeypatch.setattr(analyzer, "repair_candidate", fake_repair)
    config_path = tmp_path / "vbc.yaml"

    outcomes = analyzer.analyze_entries(
        analyzer.collect_error_entries(error_dir),
        _args(repair_ffmpeg_244=True, dry_run=True, config=config_path),
        Console(file=StringIO()),
    )

    assert outcomes[0].status == "READY"
    assert called["candidate"].error_path == error_path
    assert called["config"] == config_path
    assert called["dry_run"] is True
