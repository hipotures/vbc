"""`vbc serve` end to end: real processes, real ffmpeg/exiftool, black box."""

from __future__ import annotations

import json
import signal

import record_legacy
from conftest import REPO

EXPECTED = json.loads((REPO / "tests" / "e2e" / "expected" / "legacy_run.json").read_text())


def test_full_pass_matches_legacy(media_tree, serve_factory):
    """One pass over the fixture media produces exactly what legacy `vbc run` did."""
    root = media_tree["root"]
    before = record_legacy.snapshot(root)
    assert before == EXPECTED["inputs"]

    serve = serve_factory(["--config", str(media_tree["config"])])
    final = serve.wait_for(lambda s: s["run_status"] == "idle" and s["discovery"]["finished"])

    after = record_legacy.snapshot(root)
    assert record_legacy.diff(before, after) == EXPECTED["run1"]["changes"]
    assert after == EXPECTED["run1"]["tree"]

    counters, discovery = final["counters"], final["discovery"]
    assert (counters["completed"], counters["failed"]) == (8, 1)
    assert (discovery["files_found"], discovery["ignored_small"], discovery["source_folders_count"]) == (9, 1, 2)
    assert final["active_jobs"] == [] and final["queue"]["count"] == 0
    assert final["repair"] == {"active": False, "candidates": 1, "repaired": 0}

    errors = serve.http.get("/api/v1/errors").json()["entries"]
    assert [(e["path"].rsplit("/", 1)[-1], e["error_message"]) for e in errors] == [
        ("broken.mp4", "File is corrupted (ffprobe failed to read). Skipped.")
    ]
    recent = {job["name"] for job in serve.http.get("/api/v1/jobs/recent").json()["items"]}
    assert "vbc_tagged.mp4" in recent and "broken.mp4" in recent

    # A refresh is a second pass: nothing changes on disk.
    rev = final["rev"]
    assert serve.http.post("/api/v1/refresh").status_code == 202
    serve.wait_for(lambda s: s["rev"] > rev and s["run_status"] == "idle" and s["discovery"]["finished"])
    assert record_legacy.diff(after, record_legacy.snapshot(root)) == EXPECTED["run2"]["changes"]

    assert serve.stop() == 0


def test_commands_and_health(demo_config, serve_factory):
    """Commands are absolute/idempotent; Host guard, health and telemetry follow the contracts."""
    serve = serve_factory(["--demo", "--config", str(demo_config)])
    http = serve.http

    assert http.put("/api/v1/threads", json={"target": 99}).json()["threads"]["current"] == 8
    assert http.put("/api/v1/threads", json={"target": 2}).json()["threads"]["current"] == 2
    assert http.put("/api/v1/threads", json={"target": 2}).json()["threads"]["current"] == 2

    dirs = http.get("/api/v1/dirs")
    dirs_rev = dirs.json()["dirs_rev"]
    assert dirs.headers["etag"] == f'"{dirs_rev}"'
    assert http.put("/api/v1/dirs", json={"entries": []}).status_code == 428
    assert http.put("/api/v1/dirs", json={"entries": []}, headers={"If-Match": f'"{dirs_rev + 7}"'}).status_code == 412
    invalid = http.put("/api/v1/dirs", json={"entries": [{"path": "relative", "enabled": True}]},
                       headers={"If-Match": str(dirs_rev)})
    assert invalid.status_code == 422 and invalid.json()["errors"]

    state = http.get("/api/v1/state")
    assert http.get("/api/v1/state", headers={"If-None-Match": state.headers["etag"]}).status_code in (200, 304)
    assert http.get("/api/v1/state", params={"since": 10**9, "wait": 1}).status_code == 204
    assert http.get("/api/v1/state", headers={"Host": "evil.example"}).status_code == 403
    assert http.get("/api/v1/state", headers={"Origin": "http://evil.example"}).status_code == 403

    live = http.get("/health/live")
    assert live.status_code == 200 and live.headers["content-type"].startswith("application/health+json")
    assert "heartbeat:age" in live.json()["checks"]
    assert http.get("/health/ready").status_code == 200
    telemetry = http.get("/.well-known/homestack/status").json()
    assert telemetry["schema"] == 3 and telemetry["status"] == "pass" and telemetry["process_rss_bytes"] > 0

    assert http.post("/api/v1/shutdown", json={"mode": "graceful"}).status_code == 202
    assert http.post("/api/v1/shutdown", json={"mode": "graceful"}).status_code == 202  # idempotent
    assert serve.state()["shutdown_requested"] is True
    assert http.delete("/api/v1/shutdown").status_code == 202
    assert serve.state()["shutdown_requested"] is False

    serve.proc.send_signal(signal.SIGTERM)
    assert serve.proc.wait(timeout=60) == 0
