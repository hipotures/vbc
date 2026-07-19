import json

import pytest

from scripts import generate_metadata_manifests as generator
from scripts.generate_metadata_manifests import generate_manifests
from vbc.domain.models import CompressionManifest


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
        vbc_encoded_sources=set(),
    )

    assert result.generated == 0
    assert any("has gaps" in issue for issue in result.issues)
    assert list(metadata.glob("*.json")) == []


def test_existing_manifest_is_never_overwritten(tmp_path):
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
        vbc_encoded_sources=set(),
    )

    assert result.generated == 0
    assert result.existing_manifests == 1
    assert manifest.read_text() == "existing"


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
    encoded.write_bytes(b"encoded")
    source_snapshot = encoded.read_bytes()

    def fake_run(command, **kwargs):
        assert command[:2] == ["exiftool", "-r"]
        assert "$VBCEncoder" in command
        return generator.subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                [
                    {"SourceFile": str(encoded), "VBCEncoder": "NVENC AV1"},
                    {"SourceFile": str(tmp_path / "outside.mp4")},
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(generator.subprocess, "run", fake_run)

    assert generator.find_vbc_encoded_sources(recordings.resolve()) == {
        encoded.resolve()
    }
    assert encoded.read_bytes() == source_snapshot
