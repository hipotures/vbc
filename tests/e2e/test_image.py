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
    # Only the video folder: the manifest folder is added later through the API.
    config["input_dirs"] = [e for e in config["input_dirs"] if not e["metadata"]]
    config["general"]["log_path"] = None
    config["general"]["gpu"] = True  # no GPU in the test runtime: must fall back to CPU
    config["server"] = {"host": "0.0.0.0", "port": 8080}
    (volume / "config" / "vbc.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    before = record_legacy.snapshot(root)

    port = free_port()
    name = f"vbc-e2e-{port}"
    _run("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8080",
         "-v", f"{volume}:/home/app", "-v", f"{root}:{root}",  # identity mount for manifest paths
         "-e", "HOMESTACK_HEARTBEAT_SECONDS=10", IMAGE)  # no VBC_STORAGE_ROOTS: the mount is the root
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

        # Storage roots come from the mounts. A mount scan offers the mount's media
        # folders disabled (manifests detected); enabling one starts its work.
        assert str(root) in http.get("/api/v1/io").json()["storage_roots"]
        # The server scanned at start; a scan offers each folder once.
        scan = http.post("/api/v1/dirs/scan").json()
        assert scan["added"] == [], scan
        meta = str(media_tree["meta"])
        offered = {e["path"]: e for e in scan["entries"]}
        assert offered[meta]["enabled"] is False and offered[meta]["metadata"] is True, offered
        assert offered[str(media_tree["meta_src"])]["enabled"] is False
        current = [{"path": e["path"], "enabled": e["enabled"]} for e in scan["entries"]]
        outside = http.put("/api/v1/dirs", json={"entries": current + [{"path": "/opt", "enabled": True}]},
                           headers={"If-Match": str(scan["dirs_rev"])})
        assert outside.status_code == 422 and "outside the storage roots" in outside.text
        enable = [{**e, "enabled": e["enabled"] or e["path"] == meta} for e in current]
        added = http.put("/api/v1/dirs", json={"entries": enable}, headers={"If-Match": str(scan["dirs_rev"])})
        assert added.status_code == 200, added.text
        meta_entry = next(e for e in added.json()["entries"] if e["path"] == meta)
        assert meta_entry["enabled"] and meta_entry["metadata"] and meta_entry["watch"], meta_entry
        deadline = time.monotonic() + 300
        while record_legacy.snapshot(root) != EXPECTED["run1"]["tree"]:
            assert time.monotonic() < deadline, _run("logs", name, check=False).stdout[-3000:]
            time.sleep(1)
        saved = yaml.safe_load((volume / "config" / "vbc.yaml").read_text())
        assert {"path": meta, "enabled": True, "metadata": True}.items() <= next(
            e for e in saved["input_dirs"] if e["path"] == meta).items()
        assert meta in saved["storage"]["discovered"]

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
        assert "GPU encoding unavailable" in logs and "encoding on CPU" in logs
        assert (volume / "config" / "vbc.yaml").exists()
    finally:
        http.close()
        _run("rm", "-f", name, check=False)
