"""The VBC version written into every output file (XMP tag ``VBCVersion``).

The image sets ``VBC_VERSION`` from its release tag (``vbc-X.Y.Z`` -> ``X.Y.Z``).
A checkout derives it from git: ``0.1.7`` on a release tag, ``0.1.7-3-gabc1234``
three commits later, with ``-dirty`` for uncommitted changes.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def vbc_version() -> str:
    env = os.environ.get("VBC_VERSION", "").strip()
    if env:
        return env
    try:
        described = subprocess.run(
            ["git", "-C", str(_REPO), "describe", "--tags", "--match", "vbc-*", "--dirty"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return described.removeprefix("vbc-") or "unknown"
