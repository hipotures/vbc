"""`vbc run DIRS --config FILE`: private server + TUI in one terminal (pty)."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import struct
import sys
import termios
import time
from pathlib import Path

import httpx

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
