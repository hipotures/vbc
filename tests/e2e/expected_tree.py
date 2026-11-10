"""Expected results on the generated media set: config, tree snapshot, diff.

``expected/run.json`` holds what a pass over ``media.py`` must produce (files,
codecs, dimensions, rotation, error markers) for two passes over the same
tree. It was recorded from the pre-split application (step 2 of
docs/architecture/service-split.md) and is now a fixed expectation: a change
in output behaviour updates it deliberately, in the same commit.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from media import MIN_SIZE_BYTES

REPO = Path(__file__).resolve().parents[2]
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".flv", ".webm", ".mkv"}


def write_config(root: Path, meta: Path, videos: Path) -> Path:
    config = yaml.safe_load((REPO / "conf" / "vbc.yaml.example").read_text())
    config["input_dirs"] = [
        {"path": str(videos), "enabled": True, "metadata": False, "watch": False,
         "watch_mode": "polling", "idle_interval": None},
        {"path": str(meta), "enabled": True, "metadata": True, "watch": False,
         "watch_mode": "inotify", "idle_interval": None},
    ]
    general = config["general"]
    general.update(
        threads=2,
        gpu=False,
        log_path=str(root / "logs" / "compression.log"),
        min_size_bytes=MIN_SIZE_BYTES,
        queue_sort="name",
        wait_on_finish=False,
        bell_on_finish=False,
        debug=False,
    )
    config["gpu_config"]["enabled"] = False
    config["web_server"]["enabled"] = False
    path = root / "vbc.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"probe": "failed"}
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = [s.get("codec_name") for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    info: dict = {"audio": audio}
    if video:
        rotation = None
        for side in video.get("side_data_list", []) or []:
            if "rotation" in side:
                rotation = side["rotation"]
        info.update(
            codec=video.get("codec_name"),
            width=video.get("width"),
            height=video.get("height"),
            rotation=rotation,
        )
    duration = data.get("format", {}).get("duration")
    if duration is not None:
        info["duration_s"] = round(float(duration))
    return info


def thumbnail(path: Path) -> list[list[int]] | None:
    """First frame scaled to a 4x4 grayscale grid (orientation fingerprint)."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
         "-vf", "scale=4:4:flags=area,format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
    )
    if result.returncode != 0 or len(result.stdout) != 16:
        return None
    data = list(result.stdout)
    return [data[i * 4:(i + 1) * 4] for i in range(4)]


def _rot_cw(grid: list[list[int]]) -> list[list[int]]:
    return [list(row) for row in zip(*grid[::-1])]


def detect_rotation(source: Path, output: Path) -> int | None:
    """Clockwise angle that best maps the source picture onto the output."""
    src, out = thumbnail(source), thumbnail(output)
    if src is None or out is None:
        return None
    best, best_angle = None, None
    grid = src
    for angle in (0, 90, 180, 270):
        distance = sum(abs(a - b) for ra, rb in zip(grid, out) for a, b in zip(ra, rb))
        if best is None or distance < best:
            best, best_angle = distance, angle
        grid = _rot_cw(grid)
    return best_angle


def source_of(root: Path, output_rel: Path) -> Path | None:
    """videos_out/sub/x.mp4 -> videos/sub/x.<any extension>."""
    parts = output_rel.parts
    if not parts or not parts[0].endswith("_out"):
        return None
    src_dir = root / parts[0][: -len("_out")] / Path(*parts[1:-1]) if len(parts) > 2 else root / parts[0][: -len("_out")]
    matches = [p for p in src_dir.glob(output_rel.stem + ".*") if p.suffix.lower() in VIDEO_SUFFIXES]
    return matches[0] if len(matches) == 1 else None


def snapshot(root: Path) -> dict[str, dict]:
    """Every file under root (except config/logs) with what matters about it."""
    tree: dict[str, dict] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "vbc.yaml" or rel.startswith("logs/"):
            continue
        entry: dict = {}
        if path.suffix.lower() in VIDEO_SUFFIXES and path.stat().st_size >= 64 * 1024:
            entry = probe(path)
            source = source_of(root, Path(rel))
            if source is not None and entry.get("codec"):
                entry["source"] = source.relative_to(root).as_posix()
                entry["picture_rotation_cw"] = detect_rotation(source, path)
        elif path.suffix == ".err":
            text = path.read_text(errors="replace").replace(str(root), "<root>")
            entry = {"err_first_line": (text.strip().splitlines() or [""])[0][:200]}
        tree[rel] = entry
    return tree


def diff(before: dict[str, dict], after: dict[str, dict]) -> dict:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(k for k in set(before) & set(after) if before[k] != after[k]),
    }
