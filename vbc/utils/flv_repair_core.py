from pathlib import Path
from typing import Optional


def find_flv_header_offset(input_path: Path, chunk_size: int = 1024 * 1024) -> Optional[int]:
    """Return byte offset of the first FLV header marker in file.

    Prefers exact FLV header prefix ``b"FLV\\x01"`` and falls back to ``b"FLV"``.
    Returns ``None`` when no marker is found.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")

    primary_magic = b"FLV\x01"
    fallback_magic = b"FLV"
    overlap = b""
    consumed = 0

    with open(input_path, "rb") as src:
        while True:
            chunk = src.read(chunk_size)
            if not chunk:
                return None

            merged = overlap + chunk
            start_offset = consumed - len(overlap)

            primary_idx = merged.find(primary_magic)
            if primary_idx != -1:
                return start_offset + primary_idx

            fallback_idx = merged.find(fallback_magic)
            if fallback_idx != -1:
                return start_offset + fallback_idx

            # Keep enough bytes to detect markers split across chunk boundaries.
            overlap = merged[-3:] if len(merged) >= 3 else merged
            consumed += len(chunk)


def copy_from_offset(input_path: Path, output_path: Path, offset: int, chunk_size: int = 1024 * 1024) -> int:
    """Copy file data starting from ``offset`` into ``output_path``.

    Returns number of bytes written.
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")

    written = 0
    with open(input_path, "rb") as src, open(output_path, "wb") as dst:
        src.seek(offset)
        while True:
            block = src.read(chunk_size)
            if not block:
                break
            dst.write(block)
            written += len(block)
    return written
