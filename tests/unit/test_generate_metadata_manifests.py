import json
import os
from datetime import datetime, timezone

import pytest

from scripts import generate_metadata_manifests as generator
from scripts.generate_metadata_manifests import generate_manifests
from vbc.domain.models import CompressionManifest


SAFE_CUTOFF = datetime(2100, 1, 1, tzinfo=timezone.utc)


def test_generates_single_and_ordered_multipart_manifests_without_touching_sources(
    tmp_path,
):
    recordings = tmp_path / "recordings"
    user_dir = recordings / "user"
    user_dir.mkdir(parents=True)
    legacy = user_dir / "user_20260701_120000.mp4"
    part1 = user_dir / "user_20260702_120000_part001.mp4"
    part2 = user_dir / "user_20260702_120000_part002.mp4"
    shadowed = user_dir / "user_20260702_120000.mp4"
    legacy.write_bytes(b"legacy")
    part1.write_bytes(b"one")
    part2.write_bytes(b"two")
    shadowed.write_bytes(b"old combined")
    source_snapshot = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (legacy, part1, part2, shadowed)
    }
    compressed = tmp_path / "compressed"
    existing_output = compressed / "user" / "user_20260702_120000.mp4"
    existing_output.parent.mkdir(parents=True)
    existing_output.write_bytes(b"already encoded")
    metadata = tmp_path / "metadata"

    result = generate_manifests(
        recordings,
        metadata,
        compressed,
        modified_before=SAFE_CUTOFF,
        vbc_encoded_sources=set(),
    )

    assert result.discovered == 2
    assert result.generated == 2
    assert result.single_tasks == 1
    assert result.multipart_tasks == 1
    assert result.existing_outputs == 1
    assert result.shadowed_singles == 1
    single_payload = json.loads(
        (metadata / "ttracker-user_20260701_120000.json").read_text()
    )
    multipart_payload = json.loads(
        (metadata / "ttracker-user_20260702_120000.json").read_text()
    )
    assert single_payload["inputs"] == [str(legacy)]
    assert single_payload["output_path"] == str(
        compressed / "user" / "user_20260701_120000.mp4"
    )
    assert multipart_payload["inputs"] == [str(part1), str(part2)]
    assert multipart_payload["output_path"] == str(existing_output)
    assert multipart_payload["source_policy"] == "keep"
    CompressionManifest.model_validate(single_payload)
    CompressionManifest.model_validate(multipart_payload)
    for path, snapshot in source_snapshot.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns) == snapshot


def test_multipart_group_with_gap_is_not_generated(tmp_path):
    recordings = tmp_path / "recordings"
    user_dir = recordings / "user"
    user_dir.mkdir(parents=True)
    (user_dir / "user_20260702_120000_part001.mp4").write_bytes(b"one")
    (user_dir / "user_20260702_120000_part003.mp4").write_bytes(b"three")
    metadata = tmp_path / "metadata"

    result = generate_manifests(
        recordings,
        metadata,
        tmp_path / "compressed",
        modified_before=SAFE_CUTOFF,
        vbc_encoded_sources=set(),
    )

    assert result.generated == 0
    assert any("has gaps" in issue for issue in result.issues)
    assert list(metadata.glob("*.json")) == []


def test_different_existing_manifest_is_never_overwritten(tmp_path):
    recordings = tmp_path / "recordings"
    user_dir = recordings / "user"
    user_dir.mkdir(parents=True)
    source = user_dir / "user_20260701_120000.mp4"
    source.write_bytes(b"legacy")
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    manifest = metadata / "ttracker-user_20260701_120000.json"
    manifest.write_text("existing")

    result = generate_manifests(
        recordings,
        metadata,
        tmp_path / "compressed",
        modified_before=SAFE_CUTOFF,
        vbc_encoded_sources=set(),
    )

    assert result.generated == 0
    assert result.existing_manifests == 0
    assert result.conflicting_manifests == 1
    assert any("differs and was not overwritten" in issue for issue in result.issues)
    assert manifest.read_text() == "existing"


def test_identical_existing_manifest_is_skipped_on_repeat_scan(tmp_path):
    recordings = tmp_path / "recordings"
    user_dir = recordings / "user"
    user_dir.mkdir(parents=True)
    source = user_dir / "user_20260701_120000.mp4"
    source.write_bytes(b"legacy")
    metadata = tmp_path / "metadata"
    compressed = tmp_path / "compressed"

    first = generate_manifests(
        recordings,
        metadata,
        compressed,
        modified_before=SAFE_CUTOFF,
        vbc_encoded_sources=set(),
    )
    manifest = metadata / "ttracker-user_20260701_120000.json"
    payload = json.loads(manifest.read_text())
    payload["created_at"] = "2000-01-01T00:00:00+00:00"
    manifest.write_text(json.dumps(payload, indent=2) + "\n")
    snapshot = manifest.read_text()

    second = generate_manifests(
        recordings,
        metadata,
        compressed,
        modified_before=SAFE_CUTOFF,
        vbc_encoded_sources=set(),
    )

    assert first.generated == 1
    assert second.generated == 0
    assert second.existing_manifests == 1
    assert second.conflicting_manifests == 0
    assert second.issues == []
    assert manifest.read_text() == snapshot


def test_dry_run_does_not_create_metadata_directory(tmp_path):
    recordings = tmp_path / "recordings"
    user_dir = recordings / "user"
    user_dir.mkdir(parents=True)
    (user_dir / "user_20260701_120000.mp4").write_bytes(b"legacy")
    metadata = tmp_path / "metadata"

    result = generate_manifests(
        recordings,
        metadata,
        tmp_path / "compressed",
        modified_before=SAFE_CUTOFF,
        dry_run=True,
        vbc_encoded_sources=set(),
    )

    assert result.generated == 1
    assert not metadata.exists()


def test_refuses_to_write_metadata_inside_recordings_tree(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()

    with pytest.raises(ValueError, match="cannot be inside"):
        generate_manifests(
            recordings,
            recordings / "metadata",
            tmp_path / "compressed",
            modified_before=SAFE_CUTOFF,
            vbc_encoded_sources=set(),
        )


def test_uses_legacy_plain_file_as_first_part_when_numbering_starts_at_two(
    tmp_path,
):
    recordings = tmp_path / "recordings"
    user_dir = recordings / "user"
    user_dir.mkdir(parents=True)
    first = user_dir / "user_20260701_120000.mp4"
    part2 = user_dir / "user_20260701_120000_part002.mp4"
    part3 = user_dir / "user_20260701_120000_part003.mp4"
    first.write_bytes(b"first")
    part2.write_bytes(b"second")
    part3.write_bytes(b"third")
    metadata = tmp_path / "metadata"

    result = generate_manifests(
        recordings,
        metadata,
        tmp_path / "compressed",
        modified_before=SAFE_CUTOFF,
        vbc_encoded_sources=set(),
    )

    payload = json.loads((metadata / "ttracker-user_20260701_120000.json").read_text())
    assert payload["inputs"] == [str(first), str(part2), str(part3)]
    assert result.recovered_legacy_first_parts == 1
    assert result.shadowed_singles == 0


def test_vbc_tagged_file_is_not_used_as_a_source(tmp_path):
    recordings = tmp_path / "recordings"
    user_dir = recordings / "user"
    user_dir.mkdir(parents=True)
    encoded = user_dir / "user_20260701_120000.mp4"
    encoded.write_bytes(b"encoded")
    metadata = tmp_path / "metadata"

    result = generate_manifests(
        recordings,
        metadata,
        tmp_path / "compressed",
        modified_before=SAFE_CUTOFF,
        vbc_encoded_sources={encoded},
    )

    assert result.discovered == 0
    assert result.generated == 0
    assert result.tagged_sources == 1
    assert list(metadata.glob("*.json")) == []


def test_vbc_tag_scan_uses_exiftool_without_writing_sources(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    encoded = recordings / "encoded.mp4"
    untagged = recordings / "untagged.mp4"
    encoded.write_bytes(b"encoded")
    untagged.write_bytes(b"untagged")
    source_snapshot = encoded.read_bytes()

    def fake_run(command, **kwargs):
        assert command[:4] == ["exiftool", "-fast2", "-json", "-VBCEncoder"]
        assert command[4:] == [str(encoded), str(untagged)]
        return generator.subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                [
                    {"SourceFile": str(encoded), "VBCEncoder": "NVENC AV1"},
                    {"SourceFile": str(untagged)},
                    {"SourceFile": str(tmp_path / "outside.mp4")},
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(generator.subprocess, "run", fake_run)
    progress_updates = []

    assert generator.find_vbc_encoded_sources(
        recordings.resolve(),
        progress_callback=lambda completed, total: progress_updates.append(
            (completed, total)
        ),
    ) == {encoded.resolve()}
    assert progress_updates == [(0, 2), (2, 2)]
    assert encoded.read_bytes() == source_snapshot


def test_vbc_tag_scan_keeps_file_format_errors_for_discovery(
    tmp_path,
    monkeypatch,
):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    encoded = recordings / "encoded.mp4"
    corrupt = recordings / "corrupt.mp4"
    encoded.write_bytes(b"encoded")
    corrupt.write_bytes(b"corrupt")

    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda command, **kwargs: generator.subprocess.CompletedProcess(
            command,
            2,
            stdout=json.dumps(
                [{"SourceFile": str(encoded), "VBCEncoder": "NVENC AV1"}]
            ),
            stderr=(
                f"Error: File format error - {corrupt}\n"
                "    1 image files read\n"
            ),
        ),
    )
    issues = []

    assert generator.find_vbc_encoded_sources(
        recordings.resolve(),
        issue_callback=issues.append,
    ) == {encoded.resolve()}
    assert len(issues) == 1
    assert str(corrupt) in issues[0]
    assert "kept for manifest discovery" in issues[0]


def test_vbc_tag_scan_preserves_real_exiftool_errors(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    (recordings / "source.mp4").write_bytes(b"source")

    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda command, **kwargs: generator.subprocess.CompletedProcess(
            command,
            2,
            stdout="",
            stderr="Error: permission denied",
        ),
    )

    with pytest.raises(RuntimeError, match="permission denied"):
        generator.find_vbc_encoded_sources(recordings.resolve())


def test_modified_before_excludes_complete_task_when_any_input_is_too_new(tmp_path):
    recordings = tmp_path / "recordings"
    user_dir = recordings / "user"
    user_dir.mkdir(parents=True)
    old = user_dir / "user_20260701_120000.mp4"
    part1 = user_dir / "user_20260702_120000_part001.mp4"
    part2 = user_dir / "user_20260702_120000_part002.mp4"
    old.write_bytes(b"old")
    part1.write_bytes(b"part one")
    part2.write_bytes(b"part two still being written")
    os.utime(old, (100, 100))
    os.utime(part1, (100, 100))
    os.utime(part2, (200, 200))
    cutoff = datetime.fromtimestamp(200, tz=timezone.utc)
    metadata = tmp_path / "metadata"

    result = generate_manifests(
        recordings,
        metadata,
        tmp_path / "compressed",
        modified_before=cutoff,
        vbc_encoded_sources=set(),
    )

    assert result.generated == 1
    assert result.excluded_by_modified_before == 1
    assert (metadata / "ttracker-user_20260701_120000.json").is_file()
    assert not (metadata / "ttracker-user_20260702_120000.json").exists()


def test_modified_before_requires_timezone(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()

    with pytest.raises(ValueError, match="timezone offset"):
        generate_manifests(
            recordings,
            tmp_path / "metadata",
            tmp_path / "compressed",
            modified_before=datetime(2026, 7, 19),
            vbc_encoded_sources=set(),
        )
