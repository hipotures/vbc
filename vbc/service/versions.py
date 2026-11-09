"""Versions of what the server runs with: VBC, the tools it calls, their
libraries, the NVIDIA driver and the Python packages (GET /api/v1/versions).

Read once, on the first request, and cached: none of it changes while the
process runs (a host driver update needs a container restart anyway).
"""

from __future__ import annotations

import platform
import re
import subprocess
import threading
from importlib import metadata
from pathlib import Path
from typing import Dict, List, Optional

from vbc.version import vbc_version

_FFMPEG_LIBS = ("libavutil", "libavcodec", "libavformat", "libavdevice",
                "libavfilter", "libswscale", "libswresample")
# Debian packages that make up the image (glob patterns for dpkg-query).
_SYSTEM_PACKAGES = ("ffmpeg", "libavcodec[0-9]*", "libavformat[0-9]*", "libavfilter[0-9]*",
                    "libsvtav1enc*", "libdav1d*", "libx264-*", "libx265-*", "libvpl*",
                    "libimage-exiftool-perl", "perl", "python3", "nvtop", "libc6")
_PYTHON_PACKAGES = ("fastapi", "starlette", "uvicorn", "pydantic", "pydantic-core",
                    "httpx", "pyyaml", "pyexiftool")
_NVIDIA_BUNDLED = Path("/opt/nvidia/VERSION")

_lock = threading.Lock()
_cached: Optional[dict] = None


def _run(cmd: List[str], timeout: float = 10.0) -> Optional[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _ffmpeg(binary: str) -> tuple[Optional[str], Dict[str, str]]:
    out = _run([binary, "-hide_banner", "-version"]) or _run([binary, "-version"])
    if not out:
        return None, {}
    match = re.search(r"version\s+(\S+)", out)
    libs = {}
    for name in _FFMPEG_LIBS:
        lib = re.search(rf"^{name}\s+([\d.\s]+?)\s*/", out, re.MULTILINE)
        if lib:
            libs[name] = re.sub(r"\s+", "", lib.group(1))
    return (match.group(1) if match else out.splitlines()[0].strip()), libs


def _first_line(cmd: List[str]) -> Optional[str]:
    out = _run(cmd)
    if not out or not out.strip():
        return None
    return out.strip().splitlines()[0].strip()


def _os_name() -> Optional[str]:
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return None
    match = re.search(r'^PRETTY_NAME="?([^"\n]*)"?', text, re.MULTILINE)
    return match.group(1) if match else None


def _system_packages() -> Dict[str, str]:
    out = _run(["dpkg-query", "-W", "-f", "${db:Status-Abbrev} ${Package} ${Version}\n",
                *_SYSTEM_PACKAGES])
    if out is None:  # some pattern matched nothing (exit 1) or no dpkg: retry per pattern
        out = "".join(_run(["dpkg-query", "-W", "-f", "${db:Status-Abbrev} ${Package} ${Version}\n", p]) or ""
                      for p in _SYSTEM_PACKAGES)
    packages = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0].startswith("ii"):
            packages[parts[1]] = parts[2]
    return dict(sorted(packages.items()))


def _python_packages() -> Dict[str, str]:
    packages = {}
    for name in _PYTHON_PACKAGES:
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return packages


def _nvidia() -> Dict[str, Optional[str]]:
    try:
        bundled = _NVIDIA_BUNDLED.read_text().strip() or None
    except OSError:
        bundled = None
    driver = _first_line(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
    return {"driver": driver, "bundled_libs": bundled}


def collect() -> dict:
    """Versions as a plain dict (VersionsView fields without the envelope)."""
    global _cached
    with _lock:
        if _cached is None:
            ffmpeg, ffmpeg_libs = _ffmpeg("ffmpeg")
            ffprobe, _ = _ffmpeg("ffprobe")
            _cached = {
                "vbc": vbc_version(),
                "os": _os_name(),
                "python": platform.python_version(),
                "tools": {
                    "ffmpeg": ffmpeg,
                    "ffprobe": ffprobe,
                    "exiftool": _first_line(["exiftool", "-ver"]),
                    "nvtop": (_first_line(["nvtop", "--version"]) or "").removeprefix("nvtop version ") or None,
                },
                "ffmpeg_libs": ffmpeg_libs,
                "nvidia": _nvidia(),
                "system_packages": _system_packages(),
                "python_packages": _python_packages(),
            }
        return _cached
