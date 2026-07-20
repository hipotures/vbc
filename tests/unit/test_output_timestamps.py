import os

from vbc.pipeline.output_timestamps import apply_output_timestamps


def test_output_files_match_source_mtime_without_touching_parent(tmp_path):
    output_dir = tmp_path / "user"
    output_dir.mkdir()
    first = output_dir / "recording.mp4"
    second = output_dir / "recording_1.mp4"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    source_mtime_ns = 2_000_000_000_123_456_789
    directory_mtime_ns = 1_500_000_000_123_456_789
    os.utime(output_dir, ns=(1, directory_mtime_ns))

    update = apply_output_timestamps([first, second], source_mtime_ns)

    assert first.stat().st_mtime_ns == source_mtime_ns
    assert second.stat().st_mtime_ns == source_mtime_ns
    assert output_dir.stat().st_mtime_ns == directory_mtime_ns
    assert set(update.file_paths) == {first, second}
