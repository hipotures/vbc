import json
from types import SimpleNamespace

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


def test_multipart_size_threshold_uses_combined_source_size(tmp_path):
    source_root, compressed_root, source_user, _ = _paths(tmp_path)
    (source_user / "recording_part001.mp4").write_bytes(b"123456")
    (source_user / "recording_part002.mp4").write_bytes(b"123456")

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
    source_root.mkdir()
    compressed_root.mkdir()
    metadata_root.mkdir()
    manifest = {
        "producer": {"username": "user"},
        "output_path": str(compressed_root / "user" / "recording.mp4"),
    }
    (metadata_root / "request.json").write_text(json.dumps(manifest))
    config = SimpleNamespace(
        input_dirs=[
            SimpleNamespace(enabled=True, metadata=True, path=str(metadata_root))
        ],
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

    assert resolved == (source_root, compressed_root, 1234)
