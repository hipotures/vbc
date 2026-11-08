"""Generated media fixture set for the end-to-end suite.

Everything is produced with FFmpeg at run time (nothing binary is committed).
The set covers the discovery and processing outcomes of VBC:

videos/                      scanned input directory (metadata: false)
  clip_001.mp4               plain H.264 + AAC clip                -> encoded
  sub/clip_002.mov           MOV in a subdirectory                 -> encoded
  GOPR0001.MP4               autorotate rule 'GOPR\\d+\\.MP4' -> 180
  IMG_0001.MOV               autorotate rule 'IMG_\\d{4}\\.MOV' -> 90
  camera_model.mp4           carries a camera model tag
  av1_source.mp4             already AV1 (relevant with --skip-av1)
  vbc_tagged.mp4             carries the VBC encoder tag (treated as VBC output)
  small.mp4                  below min_size_bytes (1 MiB)          -> ignored
  broken.mp4                 random bytes                          -> error
  notes.txt                  not a video extension                 -> not scanned
meta/                        manifest input directory (metadata: true)
  req_001.json               concat_transcode of meta_src/part_*.mp4
meta_src/                    manifest inputs (not an input directory)
  part_1.mp4, part_2.mp4
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

MIN_SIZE_BYTES = 1024 * 1024
REPO = Path(__file__).resolve().parents[2]
CAMERA_MODEL = "E2E TestCam X1"
MANIFEST_OUTPUT = "meta_target/rec_20260101_120000.mp4"


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


def _exiftool(path: Path, *tags: str) -> None:
    """Write tags the way VBC reads them (custom XMP tags need VBC's config)."""
    subprocess.run(
        ["exiftool", "-config", str(REPO / "conf" / "exiftool.conf"), "-q",
         "-overwrite_original", *tags, str(path)],
        check=True,
    )


def _noisy_clip(
    path: Path,
    *,
    seconds: int = 2,
    audio: bool = True,
    vcodec: tuple[str, ...] = ("-c:v", "libx264", "-preset", "ultrafast", "-crf", "16"),
    size: str = "640x360",
) -> None:
    """Short clip with film-grain noise so it stays above 1 MiB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-f", "lavfi", "-i",
        f"testsrc2=duration={seconds}:size={size}:rate=25,noise=alls=40:allf=t",
    ]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += list(vcodec) + ["-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args += [str(path)]
    _ffmpeg(*args)


def build_media_set(root: Path) -> dict[str, Path]:
    """Create the fixture tree under ``root`` and return its key paths."""
    videos = root / "videos"
    meta = root / "meta"
    meta_src = root / "meta_src"
    for directory in (videos, meta, meta_src):
        directory.mkdir(parents=True, exist_ok=True)

    _noisy_clip(videos / "clip_001.mp4")
    _noisy_clip(videos / "sub" / "clip_002.mov")
    _noisy_clip(videos / "GOPR0001.MP4")
    _noisy_clip(videos / "IMG_0001.MOV")
    _noisy_clip(videos / "camera_model.mp4")
    _exiftool(videos / "camera_model.mp4", f"-QuickTime:Model={CAMERA_MODEL}", "-QuickTime:Make=E2E")
    _noisy_clip(
        videos / "av1_source.mp4",
        audio=False,
        vcodec=("-c:v", "libsvtav1", "-preset", "12", "-crf", "10"),
    )
    _noisy_clip(videos / "vbc_tagged.mp4")
    _exiftool(videos / "vbc_tagged.mp4", "-XMP-xmp:VBCEncoder=e2e-fixture")
    _noisy_clip(
        videos / "small.mp4",
        seconds=1,
        audio=False,
        size="160x90",
        vcodec=("-c:v", "libx264", "-preset", "ultrafast", "-crf", "35"),
    )
    (videos / "broken.mp4").write_bytes(os.urandom(MIN_SIZE_BYTES + 512 * 1024))
    (videos / "notes.txt").write_text("not a video\n")

    _noisy_clip(meta_src / "part_1.mp4")
    _noisy_clip(meta_src / "part_2.mp4")
    inputs = [str(meta_src / "part_1.mp4"), str(meta_src / "part_2.mp4")]
    manifest = {
        "schema_version": 1,
        "request_id": "e2e-req-001",
        "created_at": "2026-01-01T12:00:00Z",
        "producer": {
            "app": "ttracker",
            "username": "e2e",
            "recording_id": "rec_20260101_120000",
            "source_size_bytes": sum(Path(p).stat().st_size for p in inputs),
            "source_latest_mtime_ns": max(Path(p).stat().st_mtime_ns for p in inputs),
        },
        "operation": "concat_transcode",
        "inputs": inputs,
        "output_path": str(root / MANIFEST_OUTPUT),
        "source_policy": "keep",
        "compression_profile": "tiktok",
        "error_policy": {"missing_input": "fail"},
    }
    (meta / "req_001.json").write_text(json.dumps(manifest, indent=2) + "\n")

    small = videos / "small.mp4"
    if small.stat().st_size >= MIN_SIZE_BYTES:
        raise RuntimeError(f"{small} is not below min_size_bytes")
    for clip in videos.rglob("*"):
        if clip.is_file() and clip.name not in {"small.mp4", "notes.txt"}:
            if clip.stat().st_size < MIN_SIZE_BYTES:
                raise RuntimeError(f"{clip} is below min_size_bytes; raise its noise/bitrate")

    return {"root": root, "videos": videos, "meta": meta, "meta_src": meta_src}
