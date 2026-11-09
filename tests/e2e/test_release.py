"""Image release admission against real temporary Git repositories."""

from __future__ import annotations

import subprocess
import sys

from conftest import REPO


def test_client_only_changes_do_not_release_a_server_image(tmp_path):
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=checkout, check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init", "--bare", str(remote))
    git("init", "--initial-branch=main")
    git("config", "user.name", "VBC Test")
    git("config", "user.email", "test@example.invalid")
    git("remote", "add", "origin", str(remote))
    server = checkout / "vbc" / "service" / "__init__.py"
    client = checkout / "vbc" / "clients" / "tui" / "state.py"
    server.parent.mkdir(parents=True)
    client.parent.mkdir(parents=True)
    server.write_text('"""Server."""\n')
    client.write_text("metric = 'gpu'\n")
    git("add", ".")
    git("commit", "-m", "Baseline")
    git("tag", "vbc-0.1.0")
    git("push", "--set-upstream", "origin", "main", "--tags")

    def release_dry_run():
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "tag-image.py"), "--dry-run"],
            cwd=checkout, capture_output=True, text=True,
        )

    client.write_text("metric = 'enc'\n")
    git("commit", "-am", "Change the client graph")
    git("push")
    result = release_dry_run()
    assert result.returncode == 1, result.stderr
    assert "No server image inputs changed" in result.stderr
    assert git("tag", "--list") == "vbc-0.1.0"

    server.write_text('"""Updated server."""\n')
    git("commit", "-am", "Change the server")
    git("push")
    result = release_dry_run()
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "vbc-0.1.1"
    assert git("tag", "--list") == "vbc-0.1.0"
