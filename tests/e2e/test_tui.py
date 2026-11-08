"""`vbc tui` smoke run in a real pseudo-terminal against `serve --demo`."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import struct
import sys
import termios
import time

from conftest import REPO


def _read(master: int, buf: bytearray, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                buf.extend(os.read(master, 65536))
            except OSError:
                return


def _text(data: bytes) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", data.decode("utf-8", "replace"))


def test_tui_renders_sends_commands_and_detaches(demo_config, serve_factory):
    serve = serve_factory(["--demo", "--config", str(demo_config)])
    threads_before = serve.state()["threads"]["current"]

    pid, master = pty.fork()
    if pid == 0:  # child: the pty is its controlling terminal (Ctrl+C -> SIGINT)
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 140, 0, 0))
        os.chdir(REPO)
        os.execve(sys.executable, [sys.executable, "-m", "vbc.cli", "tui", "--api",
                                   f"http://127.0.0.1:{serve.port}"],
                  {**os.environ, "TERM": "xterm-256color"})
    buf = bytearray()
    try:
        _read(master, buf, 5)
        screen = _text(bytes(buf))
        assert "ACTIVE" in screen and "QUEUE" in screen and "Threads:" in screen
        assert "DISCONNECTED" not in screen and "Traceback" not in screen

        os.write(master, b">")
        _read(master, buf, 2)
        assert serve.state()["threads"]["current"] == threads_before + 1

        os.write(master, b"C")  # settings overlay, built from /config
        _read(master, buf, 2)
        assert "SVT-AV1" in _text(bytes(buf))

        os.write(master, b"\x03")  # Ctrl+C: detach only
        deadline = time.monotonic() + 15
        status = None
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            _read(master, buf, 0.2)
        else:
            raise AssertionError("vbc tui did not exit on Ctrl+C")
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        os.close(master)

    snapshot = serve.state()  # the server keeps running after the client detached
    assert snapshot["shutdown_requested"] is False and snapshot["run_status"] != "interrupted"
