"""End-to-end fixtures: generated media and a real `vbc serve` process.

Black box only: tests talk to the server over HTTP and look at files on disk.
See docs/development/testing.md.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

import httpx
import pytest
import yaml

E2E = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E))

import record_legacy  # noqa: E402
from media import build_media_set  # noqa: E402

REPO = E2E.parents[1]


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.e2e)


def _require_tools() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe", "exiftool") if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Serve:
    """A running `vbc serve` subprocess plus an HTTP client for it."""

    def __init__(self, args: list[str], port: int, log_path: Path, env: Optional[dict] = None) -> None:
        self.port = port
        self.log_path = log_path
        self._log = log_path.open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "vbc.cli", "serve", "--port", str(port), *args],
            cwd=REPO, stdout=self._log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env={**os.environ, "HOMESTACK_HEARTBEAT_SECONDS": "10", **(env or {})},
        )
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=45)
        self._wait_listening()

    def _wait_listening(self, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"vbc serve exited early:\n{self.log_path.read_text()}")
            try:
                self.http.get("/api/v1/state")
                return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise AssertionError(f"vbc serve did not start:\n{self.log_path.read_text()}")

    def state(self) -> dict:
        return self.http.get("/api/v1/state").json()

    def wait_for(self, predicate, timeout: float = 300) -> dict:
        """Long-poll /state until predicate(snapshot) holds."""
        deadline = time.monotonic() + timeout
        snapshot = self.state()
        while not predicate(snapshot):
            if time.monotonic() > deadline:
                raise AssertionError(f"timeout; last state: {snapshot}")
            response = self.http.get("/api/v1/state", params={"since": snapshot["rev"], "wait": 10})
            if response.status_code == 200:
                snapshot = response.json()
        return snapshot

    def stop(self, timeout: float = 120) -> int:
        """End the server process the only way it ends: a signal (SIGTERM)."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.http.close()
        self._log.close()
        return self.proc.returncode


@pytest.fixture
def media_tree(tmp_path: Path) -> dict:
    _require_tools()
    paths = build_media_set(tmp_path / "work")
    config = record_legacy.write_config(paths["root"], paths["meta"], paths["videos"])
    return {**paths, "config": config}


@pytest.fixture
def serve_factory(tmp_path: Path) -> Iterator:
    started: list[Serve] = []

    def start(args: list[str], env: Optional[dict] = None) -> Serve:
        serve = Serve(args, free_port(), tmp_path / f"serve-{len(started)}.log", env)
        started.append(serve)
        return serve

    yield start
    for serve in started:
        serve.stop()


@pytest.fixture
def fast_demo(tmp_path: Path) -> Path:
    """Demo settings whose jobs finish within about a second."""
    demo = yaml.safe_load((REPO / "conf" / "demo.yaml").read_text())
    demo["sizes"] = {"distribution": "triangular", "min_mb": 5, "mode_mb": 10, "max_mb": 20}
    demo["processing"]["throughput_mb_s"] = 40.0
    path = tmp_path / "fast-demo.yaml"
    path.write_text(yaml.safe_dump(demo, sort_keys=False))
    return path


@pytest.fixture
def demo_config(tmp_path: Path) -> Path:
    config = yaml.safe_load((REPO / "conf" / "vbc.yaml.example").read_text())
    config["input_dirs"] = []
    config["general"]["gpu"] = False
    config["gpu_config"]["enabled"] = False
    path = tmp_path / "demo-vbc.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path
