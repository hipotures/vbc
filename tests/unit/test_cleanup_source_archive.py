import json
from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from scripts import cleanup_source_archive as cleanup


def _paths(tmp_path):
    source_root = tmp_path / "sources_compressed"
    compressed_root = tmp_path / "compressed"
    source_user = source_root / "user"
    output_user = compressed_root / "user"
    source_user.mkdir(parents=True)
    output_user.mkdir(parents=True)
    return source_root, compressed_root, source_user, output_user


def test_tagged_outputs_map_only_parts_that_were_actually_used(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, output_user = _paths(tmp_path)
    sources = []
    for number in range(1, 6):
        path = source_user / f"recording_part{number:03d}.mp4"
        path.write_bytes(bytes([number]))
        sources.append(path)
    base_output = output_user / "recording.mp4"
    split_output = output_user / "recording_1.mp4"
    base_output.write_bytes(b"base")
    split_output.write_bytes(b"split")
    monkeypatch.setattr(
        cleanup,
        "_read_output_tags",
        lambda _paths: {
            base_output.resolve(): {
                "VBCEncoder": "NVENC AV1",
                "VBCSourceParts": "1,2",
            },
            split_output.resolve(): {
                "VBCEncoder": "NVENC AV1",
                "VBCSourceParts": "4,5",
            },
        },
    )

    result = cleanup.analyze_source_archive(
        source_root,
        compressed_root,
        verify_vbc_tags=True,
    )

    statuses = {decision.part_number: decision.status for decision in result.decisions}
    assert statuses == {
        1: "VERIFIED",
        2: "VERIFIED",
        3: "UNMAPPED_SOURCE",
        4: "VERIFIED",
        5: "VERIFIED",
    }


def test_tagged_output_deletes_omitted_part_only_when_probe_confirms_no_video(
    tmp_path, monkeypatch
):
    source_root, compressed_root, source_user, output_user = _paths(tmp_path)
    mapped = source_user / "recording_part001.mp4"
    no_video = source_user / "recording_part002.mp4"
    mapped.write_bytes(b"mapped")
    no_video.write_bytes(b"audio only")
    output = output_user / "recording.mp4"
    output.write_bytes(b"output")
    monkeypatch.setattr(
        cleanup,
        "_read_output_tags",
        lambda _paths: {
            output.resolve(): {
                "VBCEncoder": "NVENC AV1",
                "VBCSourceParts": "1",
            }
        },
    )
    probed = []

    def probe(self, path, scan_packet_timeline):
        probed.append(path)
        return {"has_video_stream": False, "video_packets": 0}

    monkeypatch.setattr(cleanup.FFprobeAdapter, "get_part_info", probe)

    result = cleanup.analyze_source_archive(
        source_root,
        compressed_root,
        verify_vbc_tags=True,
    )

    statuses = {decision.part_number: decision.status for decision in result.decisions}
    assert statuses == {1: "VERIFIED", 2: "IGNORED_NO_VIDEO"}
    assert probed == [no_video]
    cleanup.delete_verified_sources(result, dry_run=False)
    assert not mapped.exists()
    assert not no_video.exists()


def test_legacy_output_without_source_parts_is_supported(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, output_user = _paths(tmp_path)
    source = source_user / "recording.mp4"
    output = output_user / "recording.mp4"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    monkeypatch.setattr(
        cleanup,
        "_read_output_tags",
        lambda _paths: {output.resolve(): {}},
    )

    result = cleanup.analyze_source_archive(source_root, compressed_root)

    assert result.decisions[0].status == "LEGACY_MATCH"
    assert result.decisions[0].verified


def test_strict_legacy_check_requires_vbc_encoder(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, output_user = _paths(tmp_path)
    source = source_user / "recording.mp4"
    output = output_user / "recording.mp4"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    monkeypatch.setattr(
        cleanup,
        "_read_output_tags",
        lambda _paths: {output.resolve(): {}},
    )

    result = cleanup.analyze_source_archive(
        source_root,
        compressed_root,
        verify_vbc_tags=True,
    )

    assert result.decisions[0].status == "UNVERIFIED_OUTPUT"
    assert not result.decisions[0].verified


def test_invalid_source_parts_tag_fails_closed(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, output_user = _paths(tmp_path)
    source = source_user / "recording_part001.mp4"
    output = output_user / "recording.mp4"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    monkeypatch.setattr(
        cleanup,
        "_read_output_tags",
        lambda _paths: {
            output.resolve(): {
                "VBCEncoder": "NVENC AV1",
                "VBCSourceParts": "1,bad",
            }
        },
    )

    result = cleanup.analyze_source_archive(source_root, compressed_root)

    assert result.decisions[0].status == "INVALID_TAG"


def test_delete_verified_preserves_unmapped_sources_and_outputs(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, output_user = _paths(tmp_path)
    mapped = source_user / "recording_part001.mp4"
    unmapped = source_user / "recording_part002.mp4"
    output = output_user / "recording.mp4"
    mapped.write_bytes(b"mapped")
    unmapped.write_bytes(b"unmapped")
    output.write_bytes(b"output")
    monkeypatch.setattr(
        cleanup,
        "_read_output_tags",
        lambda _paths: {
            output.resolve(): {
                "VBCEncoder": "NVENC AV1",
                "VBCSourceParts": "1",
            }
        },
    )
    result = cleanup.analyze_source_archive(source_root, compressed_root)

    cleanup.delete_verified_sources(result, dry_run=False)

    assert result.deleted == 1
    assert not mapped.exists()
    assert unmapped.exists()
    assert output.exists()


def test_dry_run_does_not_delete_legacy_match(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, output_user = _paths(tmp_path)
    source = source_user / "recording.mp4"
    output = output_user / "recording.mp4"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    monkeypatch.setattr(
        cleanup,
        "_read_output_tags",
        lambda _paths: {output.resolve(): {"VBCEncoder": "NVENC AV1"}},
    )
    result = cleanup.analyze_source_archive(
        source_root,
        compressed_root,
        verify_vbc_tags=True,
    )

    cleanup.delete_verified_sources(result, dry_run=True)

    assert result.would_delete == 1
    assert source.exists()


def test_non_video_files_are_ignored(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, _ = _paths(tmp_path)
    marker = source_user / ".DELETE_HERE"
    marker.write_text("marker")
    monkeypatch.setattr(cleanup, "_read_output_tags", lambda _paths: {})

    result = cleanup.analyze_source_archive(source_root, compressed_root)

    assert result.decisions == []
    assert result.non_video_ignored == 1
    assert marker.exists()


def test_analysis_reports_bounded_progress(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, output_user = _paths(tmp_path)
    source = source_user / "recording.mp4"
    output = output_user / "recording.mp4"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    monkeypatch.setattr(
        cleanup,
        "_read_output_tags",
        lambda _paths, progress_callback=None: {output.resolve(): {}},
    )
    updates = []

    cleanup.analyze_source_archive(
        source_root,
        compressed_root,
        progress_callback=lambda phase, completed, total: updates.append(
            (phase, completed, total)
        ),
    )

    assert ("Locating outputs", 1, 1) in updates
    assert ("Reading VBC tags", 0, 1) in updates
    assert updates[-1] == ("Matching archived sources", 1, 1)


def test_below_minimum_group_is_deletion_eligible_without_output(tmp_path):
    source_root, compressed_root, source_user, _ = _paths(tmp_path)
    source = source_user / "recording.mp4"
    source.write_bytes(b"small")

    result = cleanup.analyze_source_archive(
        source_root,
        compressed_root,
        min_size_bytes=10,
    )

    decision = result.decisions[0]
    assert decision.status == "BELOW_MIN_SIZE"
    assert decision.size_bytes == 5
    assert decision.deletion_eligible
    cleanup.delete_verified_sources(result, dry_run=True)
    assert result.would_delete == 1
    assert source.exists()
    cleanup.delete_verified_sources(result, dry_run=False)
    assert result.deleted == 1
    assert not source.exists()


def test_multipart_size_threshold_uses_combined_source_size(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, _ = _paths(tmp_path)
    (source_user / "recording_part001.mp4").write_bytes(b"123456")
    (source_user / "recording_part002.mp4").write_bytes(b"123456")
    monkeypatch.setattr(
        cleanup.FFprobeAdapter,
        "get_part_info",
        lambda self, path, scan_packet_timeline: {
            "has_video_stream": True,
            "video_packets": 1,
        },
    )

    result = cleanup.analyze_source_archive(
        source_root,
        compressed_root,
        min_size_bytes=10,
    )

    assert {decision.status for decision in result.decisions} == {"OUTPUT_MISSING"}


def test_omitted_paths_and_size_are_resolved_from_config_and_manifests(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "sources_compressed"
    compressed_root = tmp_path / "compressed"
    metadata_root = tmp_path / "metadata"
    metadata_out = tmp_path / "metadata_out"
    source_root.mkdir()
    compressed_root.mkdir()
    metadata_root.mkdir()
    metadata_out.mkdir()
    manifest = {
        "producer": {"username": "user"},
        "output_path": str(compressed_root / "user" / "recording.mp4"),
    }
    (metadata_root / "request.json").write_text(json.dumps(manifest))
    (metadata_out / "ttracker-recording.json").write_text(json.dumps(manifest))
    config = SimpleNamespace(
        input_dirs=[
            SimpleNamespace(enabled=True, metadata=True, path=str(metadata_root))
        ],
        output_dirs=[],
        suffix_output_dirs="_out",
        metadata=SimpleNamespace(move_after_success_dir=str(source_root)),
        general=SimpleNamespace(min_size_bytes=1234),
    )
    monkeypatch.setattr(cleanup, "load_config", lambda _path: config)

    resolved = cleanup._resolve_cli_settings(
        None,
        None,
        None,
        tmp_path / "vbc.yaml",
    )

    assert resolved == (source_root, compressed_root, 1234, config)
    assert cleanup._completed_recording_ids(config) == {"recording"}


def test_default_report_hides_deletion_eligible_rows(tmp_path):
    output = tmp_path / "output.mp4"
    result = cleanup.CleanupResult(
        decisions=[
            cleanup.SourceDecision(
                tmp_path / "legacy-source.mp4",
                output,
                1,
                "LEGACY_MATCH",
                "output exists (legacy filename match)",
                (output,),
                100,
            ),
            cleanup.SourceDecision(
                tmp_path / "small-source.mp4",
                output,
                1,
                "BELOW_MIN_SIZE",
                "below threshold",
                size_bytes=100,
            ),
            cleanup.SourceDecision(
                tmp_path / "missing-source.mp4",
                output,
                1,
                "OUTPUT_MISSING",
                "base output does not exist",
                size_bytes=200,
            ),
        ]
    )
    stream = StringIO()

    cleanup._render_result(
        result,
        Console(file=stream, width=200, color_system=None),
        show_all=False,
    )

    rendered = stream.getvalue()
    assert "missing-source.mp4" in rendered
    assert "legacy-source.mp4" not in rendered
    assert "small-source.mp4" not in rendered
    assert "9261 more" not in rendered


def test_default_report_omits_empty_attention_table(tmp_path):
    result = cleanup.CleanupResult(
        decisions=[
            cleanup.SourceDecision(
                tmp_path / "ignored.mp4",
                tmp_path / "output.mp4",
                2,
                "IGNORED_NO_VIDEO",
                "no usable video packets",
            )
        ]
    )
    stream = StringIO()

    cleanup._render_result(
        result,
        Console(file=stream, width=200, color_system=None),
        show_all=False,
    )

    assert "Source verification • attention required" not in stream.getvalue()


def test_delete_limit_caps_deletion_and_lists_deleted_paths(tmp_path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    result = cleanup.CleanupResult(
        decisions=[
            cleanup.SourceDecision(
                first,
                tmp_path / "first-output.mp4",
                1,
                "BELOW_MIN_SIZE",
                "below threshold",
            ),
            cleanup.SourceDecision(
                second,
                tmp_path / "second-output.mp4",
                1,
                "BELOW_MIN_SIZE",
                "below threshold",
            ),
        ]
    )

    updates = []
    cleanup.delete_verified_sources(
        result,
        dry_run=False,
        limit=1,
        progress_callback=lambda phase, completed, total: updates.append(
            (phase, completed, total)
        ),
    )

    assert result.deleted == 1
    assert result.deleted_paths == [first]
    assert not first.exists()
    assert second.exists()
    assert updates == [
        ("Deleting sources", 0, 1),
        ("Deleting sources", 1, 1),
    ]
    stream = StringIO()
    cleanup._render_result(
        result,
        Console(file=stream, width=200, color_system=None),
        show_all=False,
    )
    rendered = stream.getvalue()
    assert "Deletion actions • limit 1" in rendered
    assert "DELETED" in rendered
    assert str(first) in rendered
    assert str(second) not in rendered


def test_delete_limit_alias_is_supported():
    args = cleanup._build_parser().parse_args(
        ["--delete-verified", "--limit-delete", "2"]
    )

    assert args.delete_limit == 2


def test_single_numeric_source_part_tag_is_valid():
    assert cleanup._parse_source_parts(1) == {1}
    assert cleanup._parse_source_parts(0) is None
    assert cleanup._parse_source_parts(True) is None


def test_completed_group_without_video_is_deletion_eligible(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, _ = _paths(tmp_path)
    source = source_user / "recording.mp4"
    source.write_bytes(b"source larger than floor")
    monkeypatch.setattr(
        cleanup.FFprobeAdapter,
        "get_part_info",
        lambda self, path, scan_packet_timeline: {
            "has_video_stream": False,
            "video_packets": 0,
        },
    )

    result = cleanup.analyze_source_archive(
        source_root,
        compressed_root,
        min_size_bytes=10,
        completed_recording_ids={"recording"},
    )

    assert result.decisions[0].status == "DONE_NO_VIDEO"
    assert result.decisions[0].deletion_eligible


def test_completed_group_with_video_remains_output_missing(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, _ = _paths(tmp_path)
    (source_user / "recording.mp4").write_bytes(b"source larger than floor")
    monkeypatch.setattr(
        cleanup.FFprobeAdapter,
        "get_part_info",
        lambda self, path, scan_packet_timeline: {
            "has_video_stream": True,
            "video_packets": 1,
        },
    )

    result = cleanup.analyze_source_archive(
        source_root,
        compressed_root,
        min_size_bytes=10,
        completed_recording_ids={"recording"},
    )

    assert result.decisions[0].status == "OUTPUT_MISSING"
    assert not result.decisions[0].deletion_eligible


def test_completed_group_uses_only_video_parts_for_size_floor(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, _ = _paths(tmp_path)
    video = source_user / "recording_part001.mp4"
    audio = source_user / "recording_part002.mp4"
    video.write_bytes(b"123456")
    audio.write_bytes(b"123456")

    def probe(self, path, scan_packet_timeline):
        if path == video:
            return {"has_video_stream": True, "video_packets": 1}
        return {"has_video_stream": False, "video_packets": 0}

    monkeypatch.setattr(cleanup.FFprobeAdapter, "get_part_info", probe)

    result = cleanup.analyze_source_archive(
        source_root,
        compressed_root,
        min_size_bytes=10,
        completed_recording_ids={"recording"},
    )

    assert {decision.status for decision in result.decisions} == {
        "DONE_BELOW_MIN_SIZE"
    }
    assert all(decision.deletion_eligible for decision in result.decisions)
    cleanup.delete_verified_sources(result, dry_run=True)
    assert result.would_delete == 2


def test_moov_failure_quarantines_source_and_metadata(tmp_path, monkeypatch):
    source_root, compressed_root, source_user, _ = _paths(tmp_path)
    metadata_root = tmp_path / "metadata"
    error_root = tmp_path / "metadata_err"
    metadata_root.mkdir()
    error_root.mkdir()
    source = source_user / "recording.mp4"
    source.write_bytes(b"source larger than floor")
    manifest = metadata_root / "ttracker-recording.json"
    marker = error_root / "ttracker-recording.err"
    manifest.write_text("{}")
    marker.write_text("moov atom not found")

    def fail_probe(self, path, scan_packet_timeline):
        raise RuntimeError("ffprobe failed: moov atom not found")

    monkeypatch.setattr(cleanup.FFprobeAdapter, "get_part_info", fail_probe)
    result = cleanup.analyze_source_archive(
        source_root,
        compressed_root,
        min_size_bytes=10,
        metadata_search_dirs=(metadata_root, error_root),
        quarantine_root=error_root,
    )

    decision = result.decisions[0]
    assert decision.status == "CORRUPT_MOOV"
    assert decision.deletion_eligible
    cleanup.delete_verified_sources(result, dry_run=False)

    destination_dir = error_root / "user"
    assert result.quarantined == 1
    assert not source.exists()
    assert not manifest.exists()
    assert not marker.exists()
    assert (destination_dir / source.name).is_file()
    assert (destination_dir / manifest.name).is_file()
    assert (destination_dir / marker.name).is_file()


def test_error_marker_quarantine_classification_is_explicit(tmp_path):
    marker = tmp_path / "request.err"
    cases = {
        "ffmpeg exited with code -6": "FFMPEG_SIGABRT",
        "ffmpeg exited with code -11": "FFMPEG_SIGSEGV",
        "Hardware is lacking required capabilities": "HARDWARE_UNSUPPORTED",
        "Manifest preflight failed: Invalid video dimensions for input.mp4: 0x0": "CORRUPT_INPUT",
        "ffprobe failed: Invalid data found when processing input": "CORRUPT_INPUT",
    }
    for error_text, expected_status in cases.items():
        marker.write_text(error_text)
        status = cleanup._quarantine_status_from_markers((marker,))
        assert status is not None
        assert status[0] == expected_status

    marker.write_text("ffmpeg exited with code 234")
    assert cleanup._quarantine_status_from_markers((marker,)) is None
