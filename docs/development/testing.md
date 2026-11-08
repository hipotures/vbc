# Testing Guide

VBC is tested **only end to end**. There are no unit tests, and none are
written for new code.

## Why no unit tests

As software grows, keeping a fine-grained test suite green costs more than the
code itself: well over half of the maintenance effort goes into tests rather
than into behaviour. The behaviour that matters in VBC is visible from outside
the process: which files are written, which are marked as errors, what the
API reports. A small suite that checks exactly that is cheaper and catches the
regressions that count.

The previous unit/integration suite (556 fast tests) was removed in step 1 of
the [service split](../architecture/service-split.md).

## The end-to-end suite

Location: `tests/e2e/`. It is created during the service split, from step 5
(the first `vbc serve` scenario). Until then, changes are checked by running
`vbc run` on a small set of media by hand.

Rules:

* **Black box.** Tests start real processes (`vbc serve` on a free loopback
  port with a temporary config, the client commands with `--json`) with real
  FFmpeg, FFprobe and ExifTool. They assert only externally visible results:
  files on disk (outputs, `.err` markers, moved error files), API counters,
  error entries, HTTP status codes. No imports of internals and no mocks.
* **Generated fixtures.** A session fixture builds a tiny media set with
  FFmpeg (`testsrc`, 1–3 s clips: normal, already compressed, below
  `min_size`, corrupt, with rotation metadata, plus a manifest). Nothing
  binary is committed.
* **Small and fast.** The whole suite runs in a few minutes. A scenario that
  keeps breaking for reasons other than real regressions is simplified or
  deleted.

Planned scenarios and the legacy comparison are listed under "Testing
strategy" in the [service split plan](../architecture/service-split.md).

## Running

```bash
uv sync --extra dev
uv run pytest            # runs tests/e2e
```

System requirements: `ffmpeg`, `ffprobe` and `exiftool` on `PATH`.

## Manual check (until the suite exists)

```bash
mkdir -p /tmp/vbc-smoke/in
ffmpeg -f lavfi -i testsrc=duration=3:size=640x360:rate=25 \
       -c:v libx264 -preset ultrafast -crf 10 /tmp/vbc-smoke/in/clip_001.mp4
head -c 300000 /dev/urandom > /tmp/vbc-smoke/in/broken_002.mp4
cp conf/vbc.yaml.example /tmp/vbc-smoke/vbc.yaml
uv run vbc run /tmp/vbc-smoke/in --config /tmp/vbc-smoke/vbc.yaml \
       --cpu --threads 2 --min-size 1 --no-wait
```

Expected: `/tmp/vbc-smoke/in_out/clip_001.mp4` exists, and
`/tmp/vbc-smoke/in_err/` holds `broken_002.mp4` with its `.err` marker.
