"""Regenerate docs/api/openapi.json from the `vbc serve` FastAPI app.

Run whenever the API changes:  uv run python scripts/export-openapi.py
The app is built in demo mode from conf/vbc.yaml.example and never started.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from vbc.service.api import create_app  # noqa: E402
from vbc.service.bootstrap import VbcServer  # noqa: E402


def main() -> int:
    server = VbcServer(
        REPO / "conf" / "vbc.yaml.example",
        demo=True,
        demo_config_path=REPO / "conf" / "demo.yaml",
        env={},
    )
    logging.getLogger().handlers.clear()
    spec = create_app(server).openapi()
    target = REPO / "docs" / "api" / "openapi.json"
    target.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {target.relative_to(REPO)} ({len(spec['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
