"""One-shot client commands against a real `vbc serve`, as separate processes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import yaml

from conftest import REPO, free_port


def vbc(serve, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-m", "vbc.cli", *args],
        cwd=REPO, capture_output=True, text=True, timeout=60,
        env={**os.environ, "VBC_API": f"http://127.0.0.1:{serve.port}"},
    )
    if check and result.returncode != 0:
        raise AssertionError(f"vbc {' '.join(args)} -> {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def test_one_shot_commands(demo_config, serve_factory, tmp_path):
    serve = serve_factory(["--demo", "--config", str(demo_config)])

    status = json.loads(vbc(serve, "status", "--json").stdout)
    assert status["schema_version"] == 1 and status["threads"]["current"] >= 1
    human = vbc(serve, "status").stdout
    assert human.startswith("VBC http://127.0.0.1:") and "Queue:" in human
    assert re.search(r"Server version: \d+\.\d+\.\d+", human), human

    assert json.loads(vbc(serve, "threads", "99", "--json").stdout)["current"] == 8
    assert vbc(serve, "threads", "3").stdout.strip() == "threads: 3"

    # dirs: add / disable / remove, persisted in the server's config file
    first, second = tmp_path / "in-a", tmp_path / "in-b"
    first.mkdir(), second.mkdir()
    vbc(serve, "dirs", "add", str(first))
    vbc(serve, "dirs", "add", str(second))
    listed = json.loads(vbc(serve, "dirs", "--json").stdout)
    assert [(e["path"], e["enabled"]) for e in listed["entries"]] == [(str(first), True), (str(second), True)]
    vbc(serve, "dirs", "disable", str(first))
    vbc(serve, "dirs", "remove", str(second))
    saved = yaml.safe_load(demo_config.read_text())["input_dirs"]
    assert [(d["path"], d["enabled"]) for d in saved] == [(str(first), False)]
    assert "[off]" in vbc(serve, "dirs").stdout

    rejected = vbc(serve, "dirs", "add", "relative/path", check=False)
    assert rejected.returncode == 1 and "must be an absolute server path" in rejected.stderr
    missing = vbc(serve, "dirs", "enable", "/not/listed", check=False)
    assert missing.returncode == 2

    assert json.loads(vbc(serve, "errors", "--json").stdout)["schema_version"] == 1
    logs = vbc(serve, "logs", "--json").stdout.splitlines()
    assert logs and all("message" in json.loads(line) for line in logs)
    assert "[server]" in vbc(serve, "config").stdout
    assert vbc(serve, "log-level", "debug").stdout.strip() == "log level: DEBUG"
    assert vbc(serve, "refresh").returncode == 0

    assert "server keeps running" in vbc(serve, "stop").stdout
    assert json.loads(vbc(serve, "status", "--json").stdout)["stop_requested"] is True
    vbc(serve, "stop", "--cancel")
    assert json.loads(vbc(serve, "status", "--json").stdout)["stop_requested"] is False
    assert vbc(serve, "shutdown", check=False).returncode == 2  # no such command any more


def test_unreachable_server_exit_code():
    port = free_port()
    result = subprocess.run(
        [sys.executable, "-m", "vbc.cli", "status", "--api", f"http://127.0.0.1:{port}"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 3 and "server unreachable" in result.stderr
