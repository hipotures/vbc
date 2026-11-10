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

import expected_tree
from conftest import REPO, free_port

IMAGE = os.environ.get("VBC_E2E_IMAGE")
RUNTIME = shutil.which("podman") or shutil.which("docker")
EXPECTED = json.loads((REPO / "tests" / "e2e" / "expected" / "run.json").read_text())

pytestmark = pytest.mark.skipif(not (IMAGE and RUNTIME), reason="set VBC_E2E_IMAGE (and have podman/docker)")


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([RUNTIME, *args], capture_output=True, text=True, check=check, timeout=300)


def test_image_refuses_without_persistent_volume():
    result = _run("run", "--rm", IMAGE, check=False)
    assert result.returncode == 1 and "not a mounted volume" in result.stderr


def test_image_contains_only_headless_runtime():
    _run("run", "--rm", "--entrypoint", "python", IMAGE, "-c", """
from importlib.util import find_spec
from pathlib import Path

for name in ('vbc.clients', 'vbc.cli', 'rich', 'typer', 'jinja2'):
    assert find_spec(name) is None, name
from vbc.service.api import create_app
from vbc.maintenance.repair import repair_candidate
from vbc.maintenance.restore import restore_failed_manifest
assert not (Path('/opt/vbc/vbc') / 'clients').exists()
""")


def test_image_full_pass_health_and_stop(media_tree, tmp_path):
    root = media_tree["root"]
    volume = tmp_path / "volume"
    (volume / "config").mkdir(parents=True)
    # Operator-edited config on the volume: the media tree (incl. the manifest dir).
    config = yaml.safe_load(media_tree["config"].read_text())
    # Only the video folder: the manifest folder is added later through the API.
    config["input_dirs"] = [e for e in config["input_dirs"] if not e["metadata"]]
    # A hand-edit typo must not stop the server: that folder is kept disabled.
    config["input_dirs"].append({"path": str(root / "typo"), "enabled": "flase"})
    config["general"]["log_path"] = None
    # An old volume config says CPU; the image's VBC_GPU=auto still probes NVENC.
    # No GPU in the test runtime: it must fall back to CPU.
    config["general"]["gpu"] = False
    config["server"] = {"host": "0.0.0.0", "port": 8080}
    (volume / "config" / "vbc.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    before = expected_tree.snapshot(root)

    readonly = tmp_path / "readonly-media" / "clips"  # an unused mount without write access
    readonly.mkdir(parents=True)
    (readonly / "a.mp4").write_bytes(b"x")
    port = free_port()
    name = f"vbc-e2e-{port}"
    _run("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8080",
         "-v", f"{volume}:/home/app", "-v", f"{root}:{root}",  # identity mount for manifest paths
         "-v", f"{readonly.parent}:/media-ro:ro",
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

        typo = next(e for e in http.get("/api/v1/dirs").json()["entries"] if e["path"] == str(root / "typo"))
        assert typo["enabled"] is False
        assert "folder kept disabled" in _run("logs", name).stdout + _run("logs", name).stderr

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
        while expected_tree.snapshot(root) != EXPECTED["run1"]["tree"]:
            assert time.monotonic() < deadline, _run("logs", name, check=False).stdout[-3000:]
            time.sleep(1)
        saved = yaml.safe_load((volume / "config" / "vbc.yaml").read_text())
        assert {"path": meta, "enabled": True, "metadata": True}.items() <= next(
            e for e in saved["input_dirs"] if e["path"] == meta).items()
        assert meta in saved["storage"]["discovered"]

        after = expected_tree.snapshot(root)
        assert expected_tree.diff(before, after) == EXPECTED["run1"]["changes"]
        assert after == EXPECTED["run1"]["tree"]

        # Manifest maintenance runs on the server without terminal libraries.
        failed_manifest = root / "meta_err" / "repair-check.json"
        failed_manifest.parent.mkdir(exist_ok=True)
        completed_manifest = next((root / "meta_out").rglob("*.json"))
        repair_manifest = json.loads(completed_manifest.read_text())
        repair_manifest["producer"]["username"] = "meta_src"
        failed_manifest.write_text(json.dumps(repair_manifest))
        failed_manifest.with_suffix(".err").write_text("ffmpeg exited with code 244\n")
        request = {"path": str(failed_manifest), "dry_run": True}
        restored = http.post("/api/v1/manifests/restore", json=request)
        assert restored.status_code == 200, restored.text
        repair = http.post("/api/v1/manifests/repair", json=request)
        assert repair.status_code == 202, repair.text
        deadline = time.monotonic() + 15
        while True:
            repair = http.get("/api/v1/manifests/repair").json()
            if repair["state"] != "running":
                break
            assert time.monotonic() < deadline, repair
            time.sleep(0.1)
        assert repair["state"] == "done", repair
        assert repair["outcomes"][0]["status"] == "READY", repair

        time.sleep(12)  # at least one heartbeat
        live = http.get("/health/live")
        assert live.status_code == 200 and live.json()["checks"]["heartbeat:count"][0]["observedValue"] >= 1
        ready = http.get("/health/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["checks"]["storage:/media-ro"][0]["status"] == "warn"
        state = http.get("/api/v1/state").json()
        assert state["encoder"] == "cpu" and state["encoder_note"], state["encoder_note"]
        gpu = http.get("/api/v1/gpu").json()  # no GPU: the metrics panel gets CPU graphs
        assert gpu["enabled"] and gpu["samples"][-1]["raw"]["device_name"].startswith("CPU"), gpu
        telemetry = http.get("/.well-known/homestack/status").json()
        assert telemetry["schema"] == 3 and telemetry["status"] == "pass"

        # Config push restarts through the headless entrypoint as well.
        saved["general"]["threads"] = 1
        pushed = http.put("/api/v1/config", json={"yaml": yaml.safe_dump(saved), "restart": True})
        assert pushed.status_code == 200, pushed.text
        deadline = time.monotonic() + 30
        while True:
            try:
                restarted = http.get("/api/v1/state").json()
                if restarted["threads"]["current"] == 1:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, _run("logs", name, check=False).stdout
            time.sleep(0.2)

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


GPU_LIBS = os.environ.get("VBC_E2E_GPU_LIBS")  # host directory with libcuda.so.*, e.g. /usr/lib


@pytest.mark.skipif(not GPU_LIBS, reason="set VBC_E2E_GPU_LIBS (host NVIDIA library dir) on a machine with a GPU")
def test_image_gpu_from_host_libraries(tmp_path):
    """The host's NVIDIA libraries, mounted read-only, make NVENC work with any
    host driver version (the bundled ones match only the owner's host)."""
    volume = tmp_path / "volume"
    volume.mkdir()
    port = free_port()
    name = f"vbc-e2e-gpu-{port}"
    devices = [arg for dev in ("/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools")
               for arg in ("--device", dev)]
    _run("run", "-d", "--name", name, *devices, "-p", f"127.0.0.1:{port}:8080",
         "-v", f"{volume}:/home/app", "-v", f"{GPU_LIBS}:/opt/host-lib:ro", IMAGE)
    try:
        deadline = time.monotonic() + 60
        while True:
            logs = _run("logs", name, check=False)
            text = logs.stdout + logs.stderr
            if "VBC server started" in text:
                break
            assert time.monotonic() < deadline, text
            time.sleep(0.5)
        assert "NVIDIA libraries from the host" in text, text
        assert "GPU encoding: av1_nvenc available" in text, text
        state = httpx.get(f"http://127.0.0.1:{port}/api/v1/state", timeout=10).json()
        assert state["encoder"] == "gpu", state
        deadline = time.monotonic() + 30  # the metrics panel gets GPU graphs (nvtop)
        while True:
            samples = httpx.get(f"http://127.0.0.1:{port}/api/v1/gpu", timeout=10).json()["samples"]
            if samples and samples[-1]["raw"]:
                break
            assert time.monotonic() < deadline, samples
            time.sleep(1)
        assert "NVIDIA" in samples[-1]["raw"]["device_name"], samples[-1]
    finally:
        _run("rm", "-f", name, check=False)
