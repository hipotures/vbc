"""The built container image (deploy/container/Containerfile), black box.

Opt-in: runs only with VBC_E2E_IMAGE set to a local image, e.g.
    podman build -f deploy/container/Containerfile -t vbc:dev .
    VBC_E2E_IMAGE=vbc:dev uv run pytest tests/e2e/test_image.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import httpx
import pytest
import yaml

import record_legacy
from conftest import REPO, free_port

IMAGE = os.environ.get("VBC_E2E_IMAGE")
RUNTIME = shutil.which("podman") or shutil.which("docker")
EXPECTED = json.loads((REPO / "tests" / "e2e" / "expected" / "legacy_run.json").read_text())

pytestmark = pytest.mark.skipif(not (IMAGE and RUNTIME), reason="set VBC_E2E_IMAGE (and have podman/docker)")


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([RUNTIME, *args], capture_output=True, text=True, check=check, timeout=300)


def test_image_refuses_without_persistent_volume():
    result = _run("run", "--rm", IMAGE, check=False)
    assert result.returncode == 1 and "not a mounted volume" in result.stderr


def test_image_full_pass_health_and_stop(media_tree, tmp_path):
    root = media_tree["root"]
    volume = tmp_path / "volume"
    (volume / "config").mkdir(parents=True)
    # Operator-edited config on the volume: the media tree (incl. the manifest dir).
    config = yaml.safe_load(media_tree["config"].read_text())
    config["general"]["log_path"] = None
    config["server"] = {"host": "0.0.0.0", "port": 8770}
    (volume / "config" / "vbc.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    before = record_legacy.snapshot(root)

    port = free_port()
    name = f"vbc-e2e-{port}"
    _run("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8770",
         "-v", f"{volume}:/home/app", "-v", f"{root}:{root}",  # identity mount for manifest paths
         "-e", f"VBC_STORAGE_ROOTS={root}", "-e", "HOMESTACK_HEARTBEAT_SECONDS=10", IMAGE)
    http = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=40)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                snapshot = http.get("/api/v1/state").json()
                break
            except httpx.HTTPError:
                assert time.monotonic() < deadline, _run("logs", name, check=False).stdout
                time.sleep(0.5)
        deadline = time.monotonic() + 300
        while not (snapshot["run_status"] == "idle" and snapshot["discovery"]["finished"]):
            assert time.monotonic() < deadline, snapshot
            response = http.get("/api/v1/state", params={"since": snapshot["rev"], "wait": 10})
            if response.status_code == 200:
                snapshot = response.json()

        after = record_legacy.snapshot(root)
        assert record_legacy.diff(before, after) == EXPECTED["run1"]["changes"]
        assert after == EXPECTED["run1"]["tree"]

        time.sleep(12)  # at least one heartbeat
        live = http.get("/health/live")
        assert live.status_code == 200 and live.json()["checks"]["heartbeat:count"][0]["observedValue"] >= 1
        ready = http.get("/health/ready")
        assert ready.status_code == 200, ready.text
        telemetry = http.get("/.well-known/homestack/status").json()
        assert telemetry["schema"] == 3 and telemetry["status"] == "pass"

        started = time.monotonic()
        _run("stop", "-t", "60", name)
        assert time.monotonic() - started < 15
        state = json.loads(_run("inspect", name, "--format", "{{json .State}}").stdout)
        assert state["ExitCode"] == 0, _run("logs", name, check=False).stdout
        logs = _run("logs", name).stdout + _run("logs", name).stderr
        assert "Traceback" not in logs
        assert (volume / "config" / "vbc.yaml").exists()
    finally:
        http.close()
        _run("rm", "-f", name, check=False)
