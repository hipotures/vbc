from pathlib import Path

from vbc.utils.flv_repair_core import copy_from_offset, find_flv_header_offset


def test_find_flv_header_offset_exact_magic(tmp_path):
    sample = tmp_path / "sample.flv"
    sample.write_bytes(b"prefix-data" + b"FLV\x01" + b"payload")

    offset = find_flv_header_offset(sample)

    assert offset == len(b"prefix-data")


def test_find_flv_header_offset_across_chunk_boundary(tmp_path):
    sample = tmp_path / "boundary.flv"
    sample.write_bytes(b"abcdeFLV\x01payload")

    offset = find_flv_header_offset(sample, chunk_size=6)

    assert offset == 5


def test_find_flv_header_offset_missing_marker(tmp_path):
    sample = tmp_path / "missing.flv"
    sample.write_bytes(b"no valid marker here")

    offset = find_flv_header_offset(sample)

    assert offset is None


def test_copy_from_offset_writes_expected_content(tmp_path):
    source = tmp_path / "src.bin"
    output = tmp_path / "out.bin"
    source.write_bytes(b"0123456789")

    written = copy_from_offset(source, output, offset=4)

    assert written == 6
    assert output.read_bytes() == b"456789"
