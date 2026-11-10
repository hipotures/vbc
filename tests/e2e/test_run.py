"""`vbc run DIRS --config FILE`: private server + TUI in one terminal (pty)."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import httpx
import yaml

from conftest import REPO
from test_tui import _read, _text


def _server_url(log: Path, timeout: float = 30) -> str:
    """The launcher writes the server command line into its log; take the port from it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists():
            found = re.findall(r"--port (\d+)", log.read_text(errors="replace"))
            if found:
                return f"http://127.0.0.1:{found[-1]}"
        time.sleep(0.2)
    raise AssertionError("vbc run did not start a server")


def test_run_private_server_with_folders_from_args(media_tree, tmp_path):
    config: Path = media_tree["config"]
    config_before = config.read_bytes()
    videos = media_tree["videos"]
    state_home = tmp_path / "state"

    pid, master = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 140, 0, 0))
        os.chdir(REPO)
        os.execve(sys.executable, [sys.executable, "-m", "vbc.cli", "run", str(videos), "--config", str(config)],
                  {**os.environ, "TERM": "xterm-256color", "XDG_STATE_HOME": str(state_home)})
    buf = bytearray()
    try:
        url = _server_url(state_home / "vbc" / "run-server.log")
        http = httpx.Client(base_url=url, timeout=30)
        deadline = time.monotonic() + 90
        while True:  # the server starts after the launcher logged its port
            try:
                snapshot = http.get("/api/v1/state").json()
                break
            except httpx.HTTPError:
                assert time.monotonic() < deadline, "private server did not answer"
                _read(master, buf, 0.3)

        # Only the folder from the command line, in memory; the Dirs list is read-only.
        dirs = http.get("/api/v1/dirs").json()
        assert [e["path"] for e in dirs["entries"]] == [str(videos.resolve())]
        rejected = http.put("/api/v1/dirs", json={"entries": []}, headers={"If-Match": str(dirs["dirs_rev"])})
        assert rejected.status_code == 409
        assert http.post("/api/v1/dirs/scan").status_code == 409

        # The dashboard runs in this terminal against the private server.
        deadline = time.monotonic() + 120
        while not snapshot.get("active_jobs") and snapshot["run_status"] == "discovering":
            assert time.monotonic() < deadline, f"no work started: {snapshot}"
            _read(master, buf, 0.5)
            snapshot = http.get("/api/v1/state").json()
        _read(master, buf, 2)
        screen = _text(bytes(buf))
        assert "ACTIVE" in screen and "QUEUE" in screen and "Traceback" not in screen

        # q quits the dashboard and stops the server (running jobs are interrupted).
        os.write(master, b"q")
        deadline = time.monotonic() + 150
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            assert time.monotonic() < deadline, "vbc run did not exit after q"
            _read(master, buf, 0.2)
        assert os.waitstatus_to_exitcode(status) == 0
        try:
            http.get("/api/v1/state", timeout=2)
            raise AssertionError("the private server is still running after vbc run exited")
        except httpx.HTTPError:
            pass
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        os.close(master)

    assert config.read_bytes() == config_before, "vbc run must not change the config file"
    assert (media_tree["meta"] / "req_001.json").exists(), \
        "a folder from the config was processed although DIRS replaced the list"


def _start_run(args: list[str], state_home: Path) -> tuple[int, int]:
    pid, master = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 140, 0, 0))
        os.chdir(REPO)
        os.execve(sys.executable, [sys.executable, "-m", "vbc.cli", "run", *args],
                  {**os.environ, "TERM": "xterm-256color", "XDG_STATE_HOME": str(state_home)})
    return pid, master


def _wait_api(http: httpx.Client, master: int, buf: bytearray, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return http.get("/api/v1/state").json()
        except httpx.HTTPError:
            assert time.monotonic() < deadline, "private server did not answer"
            _read(master, buf, 0.3)


def test_run_config_push_restart_keeps_folders_from_args(media_tree, tmp_path):
    """A config push makes the private server restart; it comes back with the
    folder from the command line, the pushed settings, and the file's own list."""
    config: Path = media_tree["config"]
    videos = media_tree["videos"]
    state_home = tmp_path / "state"
    log = state_home / "vbc" / "run-server.log"
    file_dirs = yaml.safe_load(config.read_text())["input_dirs"]

    pid, master = _start_run([str(videos), "--config", str(config)], state_home)
    buf = bytearray()
    try:
        url = _server_url(log)
        http = httpx.Client(base_url=url, timeout=30)
        _wait_api(http, master, buf)
        assert http.get("/api/v1/config/values").json()["values"]["general"]["threads"] == 2

        pushed = yaml.safe_load(config.read_text())
        pushed["general"]["threads"] = 1
        pushed["input_dirs"] = [{"path": str(tmp_path / "client-only"), "enabled": True}]
        client_file = tmp_path / "pushed.yaml"
        client_file.write_text(yaml.safe_dump(pushed, sort_keys=False))
        result = subprocess.run(
            [sys.executable, "-m", "vbc.cli", "config", "--api", url, "--push", str(client_file)],
            cwd=REPO, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        # Running jobs finish, then the server re-executes itself on the same port.
        deadline = time.monotonic() + 300
        while log.read_text(errors="replace").count("VBC server started") < 2:
            assert time.monotonic() < deadline, f"no restart:\n{log.read_text(errors='replace')[-3000:]}"
            _read(master, buf, 0.5)
        _wait_api(http, master, buf)

        dirs = http.get("/api/v1/dirs").json()
        assert [e["path"] for e in dirs["entries"]] == [str(videos.resolve())]
        assert http.put("/api/v1/dirs", json={"entries": []},
                        headers={"If-Match": str(dirs["dirs_rev"])}).status_code == 409
        assert http.get("/api/v1/config/values").json()["values"]["general"]["threads"] == 1
        assert yaml.safe_load(config.read_text())["input_dirs"] == file_dirs, \
            "the push or the restart changed the config file's folder list"

        os.write(master, b"q")
        deadline = time.monotonic() + 150
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            assert time.monotonic() < deadline, "vbc run did not exit after q"
            _read(master, buf, 0.2)
        assert os.waitstatus_to_exitcode(status) == 0
        try:
            http.get("/api/v1/state", timeout=2)
            raise AssertionError("the restarted private server is still running after vbc run exited")
        except httpx.HTTPError:
            pass
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        os.close(master)

    assert (media_tree["meta"] / "req_001.json").exists(), "the config's other folder was processed"
