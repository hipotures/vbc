"""`vbc serve` end to end: real processes, real ffmpeg/exiftool, black box."""

from __future__ import annotations

import json
import re
import signal
import subprocess
import time

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

    # Every output names the VBC version that produced it (git describe here).
    output = next((root / "videos_out").rglob("clip_001.mp4"))
    version = subprocess.run(
        ["exiftool", "-config", str(REPO / "conf" / "exiftool.conf"), "-s3", "-XMP:VBCVersion", str(output)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert re.match(r"\d+\.\d+\.\d+", version), version

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

    assert http.post("/api/v1/shutdown").status_code in (404, 405)  # the API never ends the server

    serve.proc.send_signal(signal.SIGTERM)
    assert serve.proc.wait(timeout=60) == 0


def test_stop_keeps_the_server_running(demo_config, fast_demo, serve_factory):
    """POST /stop stops processing only; refresh resumes in the same process."""
    serve = serve_factory(["--demo", "--config", str(demo_config), "--demo-config", str(fast_demo)])
    http = serve.http
    serve.wait_for(lambda s: s["run_status"] == "running" and s["active_jobs"], timeout=30)

    # graceful stop, then cancel: processing continues
    assert http.post("/api/v1/stop", json={"mode": "graceful"}).status_code == 202
    assert http.post("/api/v1/stop", json={"mode": "graceful"}).status_code == 202  # idempotent
    assert serve.state()["stop_requested"] is True
    assert http.delete("/api/v1/stop").status_code == 202
    assert serve.state()["stop_requested"] is False

    # graceful stop: running jobs finish, nothing new starts, the server stays up
    http.post("/api/v1/stop", json={"mode": "graceful"})
    stopped = serve.wait_for(lambda s: s["run_status"] == "stopped", timeout=60)
    assert stopped["active_jobs"] == []
    time.sleep(2)
    assert serve.proc.poll() is None and serve.state()["counters"] == stopped["counters"]

    # refresh resumes processing in the same process
    assert http.post("/api/v1/refresh").status_code == 202
    serve.wait_for(lambda s: s["run_status"] == "running" and s["active_jobs"], timeout=30)

    # interrupt: running jobs are stopped now, the server stays up
    assert http.post("/api/v1/stop", json={"mode": "interrupt"}).status_code == 202
    serve.wait_for(lambda s: s["run_status"] == "stopped", timeout=30)
    assert serve.proc.poll() is None

    serve.proc.send_signal(signal.SIGTERM)
    assert serve.proc.wait(timeout=60) == 0


def test_shutdown_with_open_long_poll_is_clean(demo_config, serve_factory):
    """A client waiting in a long-poll must not delay or break the shutdown."""
    import threading
    import time

    serve = serve_factory(["--demo", "--config", str(demo_config)])
    rev = serve.state()["rev"]
    answers = []

    def long_poll():
        answers.append(serve.http.get("/api/v1/state", params={"since": 10**9, "wait": 30}).status_code)

    waiter = threading.Thread(target=long_poll)
    waiter.start()
    time.sleep(1)
    started = time.monotonic()
    serve.proc.send_signal(signal.SIGINT)
    assert serve.proc.wait(timeout=30) == 0
    waiter.join(timeout=10)
    assert time.monotonic() - started < 6, "shutdown waited for the long-poll"
    assert answers == [204]
    log = serve.log_path.read_text()
    assert "Traceback" not in log and "timeout graceful shutdown exceeded" not in log
    assert rev >= 1


def test_stop_and_resume_on_real_media(media_tree, serve_factory):
    """Interrupt mid-pass, then refresh: the end result on disk is still the legacy one."""
    root = media_tree["root"]
    before = record_legacy.snapshot(root)
    serve = serve_factory(["--config", str(media_tree["config"])])
    serve.wait_for(lambda s: s["active_jobs"], timeout=120)
    assert serve.http.post("/api/v1/stop", json={"mode": "interrupt"}).status_code == 202
    serve.wait_for(lambda s: s["run_status"] == "stopped", timeout=120)
    assert serve.proc.poll() is None

    assert serve.http.post("/api/v1/refresh").status_code == 202
    serve.wait_for(lambda s: s["run_status"] == "idle" and s["discovery"]["finished"], timeout=300)
    after = record_legacy.snapshot(root)
    assert record_legacy.diff(before, after) == EXPECTED["run1"]["changes"]
    assert after == EXPECTED["run1"]["tree"]


def test_stopped_server_ignores_new_files_until_refresh(media_tree, serve_factory):
    """STOP: a watched folder getting a new file starts nothing; R (refresh) resumes."""
    import shutil

    import yaml

    config = yaml.safe_load(media_tree["config"].read_text())
    config["input_dirs"] = [
        {**entry, "watch": True, "watch_mode": "polling", "poll_interval_seconds": 1}
        if not entry["metadata"] else entry
        for entry in config["input_dirs"]
    ]
    media_tree["config"].write_text(yaml.safe_dump(config, sort_keys=False))
    videos = media_tree["videos"]
    serve = serve_factory(["--config", str(media_tree["config"])])
    serve.wait_for(lambda s: s["run_status"] == "idle" and s["discovery"]["finished"], timeout=300)

    assert serve.http.post("/api/v1/stop", json={"mode": "graceful"}).status_code == 202
    stopped = serve.wait_for(lambda s: s["run_status"] == "stopped", timeout=60)

    shutil.copy(videos / "clip_001.mp4", videos / "late_arrival.mp4")
    time.sleep(5)  # several polling intervals
    state = serve.state()
    assert state["run_status"] == "stopped" and state["counters"] == stopped["counters"]
    assert not list((videos.parent / "videos_out").rglob("late_arrival.mp4"))

    assert serve.http.post("/api/v1/refresh").status_code == 202
    serve.wait_for(
        lambda s: s["run_status"] == "idle" and s["counters"]["completed"] > stopped["counters"]["completed"],
        timeout=300,
    )
    assert list((videos.parent / "videos_out").rglob("late_arrival.mp4"))


def test_watched_folder_feeds_the_queue_while_jobs_run(tmp_path, serve_factory):
    """A file arriving in a watched folder joins the queue while a job is still
    encoding (no manual refresh, no waiting for an empty queue)."""
    import shutil

    import yaml

    from media import _noisy_clip

    videos = tmp_path / "w" / "videos"
    _noisy_clip(videos / "long.mp4", seconds=40, size="1280x720")
    _noisy_clip(tmp_path / "late.mp4", seconds=2)
    root = tmp_path / "w"
    config = record_legacy.write_config(root, root / "meta", videos)
    data = yaml.safe_load(config.read_text())
    data["general"]["threads"] = 1
    data["input_dirs"] = [
        {"path": str(videos), "enabled": True, "watch": True,
         "watch_mode": "polling", "poll_interval_seconds": 1}
    ]
    config.write_text(yaml.safe_dump(data, sort_keys=False))

    serve = serve_factory(["--config", str(config)])
    serve.wait_for(lambda s: s["active_jobs"], timeout=120)
    shutil.copy(tmp_path / "late.mp4", videos / "late_arrival.mp4")

    state = serve.wait_for(
        lambda s: s["queue"]["count"] >= 1 or s["counters"]["completed"] >= 1, timeout=60
    )
    assert state["active_jobs"], "the long job finished before the late file was seen"
    assert state["queue"]["count"] == 1
    page = serve.http.get("/api/v1/queue").json()
    assert [i["name"] for i in page["items"]] == ["late_arrival.mp4"]
