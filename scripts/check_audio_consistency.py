#!/usr/bin/env python3
"""Check audio handling between input and output directories.

Compares input audio codecs to expected output behavior based on VBC rules:
- Lossless (pcm_*, flac, alac, truehd, mlp, wavpack, ape, tta) -> AAC 256k
- AAC/MP3 -> stream copy
- Other/unknown -> AAC 192k
- No audio -> no audio
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple


DEFAULT_EXTS = {
    ".mp4",
    ".mov",
    ".avi",
    ".flv",
    ".webm",
    ".mkv",
    ".m4v",
    ".mts",
    ".m2ts",
}

LOSSLESS_CODECS = {"flac", "alac", "truehd", "mlp", "wavpack", "ape", "tta"}
COPY_CODECS = {"aac", "mp3"}


def _normalize_codec(raw: str) -> str:
    if not raw:
        return ""
    return re.split(r"[,\s(]", raw.lower(), maxsplit=1)[0]


def _probe_audio(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,bit_rate,channels",
        "-of", "json",
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {"error": (res.stderr or "").strip()}
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": "invalid_json"}
    streams = data.get("streams", [])
    if not streams:
        return {"codec": None}
    stream = streams[0] or {}
    codec = stream.get("codec_name") or "unknown"
    bit_rate = stream.get("bit_rate")
    try:
        bit_rate = int(bit_rate) if bit_rate is not None else None
    except (TypeError, ValueError):
        bit_rate = None
    return {
        "codec": codec,
        "bit_rate": bit_rate,
        "channels": stream.get("channels"),
    }


def _expected_for_input(codec: str) -> Tuple[str, Optional[int]]:
    audio_codec = _normalize_codec(codec)
    if not audio_codec:
        return ("aac", 192000)
    if audio_codec.startswith("pcm_") or audio_codec in LOSSLESS_CODECS:
        return ("aac", 256000)
    if audio_codec in COPY_CODECS:
        return (audio_codec, None)
    return ("aac", 192000)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify audio handling between input and output directories."
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Input directory containing source videos.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: <input_dir>_out).",
    )
    parser.add_argument(
        "--bitrate-tolerance",
        type=float,
        default=0.10,
        help="Allowed AAC bitrate deviation ratio (default: 0.10).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    input_dir = args.input_dir
    output_dir = args.output_dir or input_dir.with_name(f"{input_dir.name}_out")

    if not input_dir.exists():
        print(f"Input dir not found: {input_dir}")
        return 2
    if not output_dir.exists():
        print(f"Output dir not found: {output_dir}")
        return 2

    inputs = [
        p
        for p in input_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in DEFAULT_EXTS
    ]

    missing_outputs = []
    probe_errors = []
    no_audio_inputs = 0
    no_audio_outputs = 0
    lossless_inputs = 0
    copy_inputs = 0
    transcode_inputs = 0
    unknown_inputs = 0
    mismatches = []

    for inp in inputs:
        rel = inp.relative_to(input_dir)
        out = (output_dir / rel).with_suffix(".mp4")
        if not out.exists():
            missing_outputs.append((inp, out))
            continue

        in_audio = _probe_audio(inp)
        out_audio = _probe_audio(out)

        if "error" in in_audio:
            probe_errors.append((inp, "input", in_audio["error"]))
            continue
        if "error" in out_audio:
            probe_errors.append((out, "output", out_audio["error"]))
            continue

        in_codec = in_audio.get("codec")
        out_codec = out_audio.get("codec")

        if in_codec is None:
            no_audio_inputs += 1
            if out_codec is None:
                no_audio_outputs += 1
            else:
                mismatches.append((inp, out, in_audio, out_audio, "input has no audio, output has audio"))
            continue

        normalized = _normalize_codec(in_codec)
        if not normalized:
            unknown_inputs += 1
        elif normalized.startswith("pcm_") or normalized in LOSSLESS_CODECS:
            lossless_inputs += 1
        elif normalized in COPY_CODECS:
            copy_inputs += 1
        else:
            transcode_inputs += 1

        expected_codec, expected_br = _expected_for_input(in_codec)

        if out_codec is None:
            mismatches.append((inp, out, in_audio, out_audio, "output missing audio"))
            continue

        if out_codec != expected_codec:
            mismatches.append((inp, out, in_audio, out_audio, f"expected {expected_codec}"))
            continue

        if expected_codec == "aac" and expected_br is not None:
            out_br = out_audio.get("bit_rate")
            if out_br is not None:
                tolerance = args.bitrate_tolerance
                if abs(out_br - expected_br) > expected_br * tolerance:
                    mismatches.append(
                        (inp, out, in_audio, out_audio, f"aac bitrate outside {int(tolerance * 100)}%")
                    )

    print(f"Inputs scanned: {len(inputs)}")
    print(f"Missing outputs: {len(missing_outputs)}")
    print(f"Probe errors: {len(probe_errors)}")
    print(f"Inputs w/o audio: {no_audio_inputs}")
    print(f"Outputs w/o audio (matching no-audio inputs): {no_audio_outputs}")
    print(f"Lossless/PCM inputs: {lossless_inputs}")
    print(f"Copy inputs (aac/mp3): {copy_inputs}")
    print(f"Transcode inputs (other): {transcode_inputs}")
    print(f"Unknown audio codec inputs: {unknown_inputs}")
    print(f"Audio mismatches: {len(mismatches)}")

    if missing_outputs:
        print("\nMissing outputs (input -> expected output):")
        for inp, out in missing_outputs:
            print(f"- {inp} -> {out}")

    if probe_errors:
        print("\nffprobe errors:")
        for path, side, err in probe_errors:
            print(f"- {side}: {path} :: {err}")

    if mismatches:
        print("\nAudio mismatches:")
        for inp, out, in_audio, out_audio, reason in mismatches:
            print(f"- {inp} -> {out} :: {reason}")
            print(f"  in={in_audio} out={out_audio}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
