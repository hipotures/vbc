"""`vbc run [DIRS] [--config FILE]`: a private server plus `vbc tui` in one terminal.

The launcher starts `vbc serve` as a child process on loopback (free port),
attaches the TUI to it and stops the server when the TUI quits. Folders given
on the command line replace the config's list for this run only (the server
never saves them and keeps its Dirs list read-only); without folders the
config's list applies. Every other setting comes from the config file and the
per-folder ``VBC.YAML`` files: there are no per-run options.

Like every client, the launcher talks to the server only over HTTP; it never
imports server modules.
"""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

import httpx

from vbc.clients.tui.app import default_log_file, run_tui

START_TIMEOUT_S = 90.0      # the GPU probe alone may take up to 30 s
GRACEFUL_WAIT_S = 3.0       # first SIGINT: an idle server ends at once
INTERRUPT_WAIT_S = 120.0    # second SIGINT: running jobs are interrupted


def server_log_file() -> Path:
    """Next to the TUI log: $XDG_STATE_HOME/vbc/run-server.log."""
    return default_log_file().with_name("run-server.log")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _tail(path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _wait_ready(proc: subprocess.Popen, url: str) -> bool:
    deadline = time.monotonic() + START_TIMEOUT_S
    with httpx.Client(base_url=url, timeout=2) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            try:
                client.get("/api/v1/state")
                return True
            except httpx.HTTPError:
                time.sleep(0.2)
    return False


def _stop_server(proc: subprocess.Popen) -> int:
    """SIGINT ends an idle server; a second SIGINT interrupts running jobs."""
    for wait_s in (GRACEFUL_WAIT_S, INTERRUPT_WAIT_S):
        if proc.poll() is not None:
            break
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            if wait_s == GRACEFUL_WAIT_S:
                print("vbc run: interrupting running jobs...", file=sys.stderr, flush=True)
    if proc.poll() is None:
        proc.kill()
    return proc.wait()


def run(dirs: List[Path], config: Path, log_file: Optional[str] = None) -> int:
    if not config.exists():
        print(f"vbc run: config file not found: {config}", file=sys.stderr)
        return 2
    for path in dirs:
        if not path.is_dir():
            print(f"vbc run: not a directory: {path}", file=sys.stderr)
            return 2

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log_path = server_log_file()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "vbc.service", "--config", str(config.resolve()),
               "--host", "127.0.0.1", "--port", str(port), *(str(p.resolve()) for p in dirs)]
    with log_path.open("a") as log:
        log.write(f"\n=== vbc run {time.strftime('%Y-%m-%d %H:%M:%S')}: {' '.join(command[2:])}\n")
        log.flush()
        # Own session: terminal signals reach the launcher only; it stops the server.
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    try:
        if not _wait_ready(proc, url):
            code = proc.poll()
            reason = f"exited with code {code}" if code is not None else "did not start in time"
            print(f"vbc run: the server {reason}; log {log_path}:\n{_tail(log_path)}", file=sys.stderr)
            return 2
        return run_tui(url, log_file)
    finally:
        _stop_server(proc)
