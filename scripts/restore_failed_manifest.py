#!/usr/bin/env python3
"""Command-line wrapper; the logic lives in vbc.maintenance.restore (also used by `vbc manifests restore`)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vbc.maintenance.restore import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
