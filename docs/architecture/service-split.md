# Service Split Plan (server / API / clients)

Status: **proposal**. Scope: VBC only. Phase 1 assumes the server and the
clients run as ordinary local processes that may later live on different
hosts. The final deployment target is **HomeStack** (an unprivileged Proxmox
LXC built from an OCI image, like the owner's other services); its
requirements are collected in [Deployment: HomeStack](#deployment-homestack)
and shape the server from step 5 on. GPU device handling inside the container
is out of scope for now.

Order of delivery (decided): **server + CLI client + TUI client first**, the
web client later. The existing Rich TUI is reused as an API client (same
panels, data from the API instead of the in-process `UIState`); it is not
redesigned.

## Goal

```
┌──────────────────────── vbc serve (headless) ────────────────────────┐
│ Orchestrator ─ FFmpeg/FFprobe/ExifTool ─ ManifestWatcher ─ Repair    │
│        │ publish()                ▲ publish(command events)          │
│        ▼                          │                                  │
│   EventBus (in-process, sync) ────┼──────────────┐                   │
│        │                          │              │                   │
│        ▼                          │              │                   │
│   StateStore (read model, rev)  CommandService (validate, persist)   │
│        │                          ▲                                  │
│        └──────── HTTP JSON API /api/v1 ──────────┘                   │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │ HTTP (long-poll), no auth   
            ┌──────────────────────┼──────────────────────┐
            ▼                      ▼                      ▼
     vbc tui (Rich,          vbc status/threads/     vbc-web (later,
     phase 1)                refresh/… (one-shot     own process/port)
                             CLI, phase 1)           + curl / scripts
```

* The **server** owns all business state and all side effects (encoding, config
  writes, marker files, repair).
* **Clients** own only presentation state (tabs, cursors, palettes, pagination,
  formatting) and never import server internals.
* The in-process `EventBus` stays the backbone inside the server. It is **not**
  exported over the network; the network sees a versioned read model and a small
  command API.
* **Control plane only, no data plane.** Clients only *steer* the server
  (add/remove/reorder/enable input folders, threads, refresh, stop, log
  level) and *read status*. Video files are never transferred: there is no
  upload, download or streaming of media through the API, and clients need no
  access to the files at all. The server reads inputs and writes outputs,
  error files and markers only in its own filesystem; every path a client
  sees or sends is a server path. What clients do see is metadata the server
  derives (file names and paths, sizes, codec/resolution/duration, progress,
  error messages). Moving media between client and server is out of scope for
  this plan.

## Current state (findings)

What already works in our favour:

* `pipeline/orchestrator.py` has no imports from `vbc.ui` or `rich`; it talks
  only through the `EventBus`.
* Runtime control already arrives as events: `ThreadControlEvent`,
  `RequestShutdown`, `InterruptRequested`, `RefreshRequested`,
  `InputDirsChanged` (`orchestrator.py:246-250`).
* Events and domain models are Pydantic, so serialisation is trivial.
* Long-running operation exists: `input_dirs.watch` (inotify/polling) plus
  `general.wait_on_finish`.
* `KeyboardListener` already no-ops without a TTY; `DemoOrchestrator` gives a
  realistic event stream without touching files (ideal for client development).

Layer leaks that block the split:

| Leak | Location | Why it matters |
|---|---|---|
| Business read model lives in the UI layer | `ui/state.py` (`UIState`) mixes counters/jobs/queue with overlay tabs, cursors, palettes | The server has nothing to serve without importing `vbc.ui` |
| UI layer performs a business side effect | `ui/manager.py` `on_dirs_apply_changes` calls `save_dirs_config()` and publishes `InputDirsChanged` | Config persistence must belong to the server |
| Infrastructure depends on UI | `infrastructure/gpu_monitor.py` imports `UIState` at runtime and **writes** `gpu_data` / `gpu_history_*` into it under `UIState._lock`; `infrastructure/web_server.py` imports it only under `TYPE_CHECKING` but reads the instance injected by `main.py` | Inverted dependency; the GPU producer has no home outside the UI |
| Presentation outside `ui/` | `pipeline/repair.py`, `pipeline/error_file_mover.py` (`rich.console` / `rich.progress`); also `config/input_dirs.py` (`rich.markup.escape`) and `utils/fix_vbc_tags.py` (`rich.progress`, CLI utility) | Headless server must not print Rich output or emit Rich markup |
| TUI-only events in the domain | `domain/events.py` `Dirs*` (overlay/sparkline events already live in `ui/keyboard.py`) | Keystroke-level events are not domain events |
| Composition root is one function | `main.py` `compress()` (the single Typer command) always builds Dashboard, Keyboard, UIManager, GpuMonitor, web server | No headless entry point |
| Deltas, not values, for threads | `ThreadControlEvent(change=±1)`, clamped to a hard-coded `1..8` in `Orchestrator._on_thread_control` | Two clients pressing `>` race; network commands must be absolute and idempotent, and clients need the limits |
| Toggle, not value, for shutdown | `RequestShutdown`: the first one requests a graceful shutdown, the second one **cancels** it (`Orchestrator._on_shutdown_request`) | A retried or duplicated `POST /shutdown` silently cancels the shutdown |
| No job identity | `VideoFile` / `CompressionJob` have no id; jobs are identified by path | `/state`, `/errors` and `/logs` need a stable `job_id` |
| View code reads business state | `KeyboardListener` reads `UIState` to route keys while the Dirs tab is active | After the split it must read view state, not `StateStore` |

## Code strategy: legacy package + new tree

The whole current package moves to `vbc_legacy/` (`git mv`, absolute imports
rewritten `vbc.` → `vbc_legacy.`); today's test suite is deleted (see Testing
strategy). The new
`vbc/` is built from scratch next to it, **taking over from legacy only what
is needed**, by copying whole files (`cp`) or cutting out fragments. When the
new clients reach parity, `vbc_legacy/` is deleted in one commit, so no old code survives that was not deliberately carried over.

```
vbc_legacy/      # frozen copy of today's vbc/ — `vbc run <dirs>` runs from here during migration
vbc/             # new: domain, config, infrastructure, pipeline, service, contracts, clients
tests/e2e/       # the only tests: black-box end-to-end (see Testing strategy)
```

Rules:

* **Legacy is frozen.** No features; a critical bug fix is made in both trees.
* **Two separate packages.** Legacy and new code never import each other;
  the only bridge is the e2e fixture set run against both.
* **Copy almost whole, then modify:** `domain/`, `config/`, the FFmpeg /
  FFprobe / ExifTool adapters, watchers, `event_bus`, `logging`,
  `pipeline/orchestrator.py`, `pipeline/repair.py`,
  `pipeline/error_file_mover.py`. This code does not depend on the UI;
  rewriting it piecemeal risks losing edge cases (hardware-capability
  detection, CPU fallback, manifests, repair, verification, rotation,
  metadata, colour remux). The layer leaks listed above are fixed in the copy.
* **Cut out fragments:** `UIManager` handlers → `service/state_store.py`,
  `on_dirs_apply_changes` → `service/commands.py`, wiring from `compress()`
  → `service/bootstrap.py`, Rich panels / overlays / `format_*` helpers →
  `clients/tui/`.
* **Write new:** `contracts/`, `service/api.py`, `clients/api_client.py`,
  `clients/commands.py`, `vbc/cli.py`.
* **Not carried over:** `UIState`, `UIManager`, `infrastructure/web_server.py`,
  `parse_config_lines`, the `Dirs*` domain events, the delta/toggle command
  events. They disappear with `vbc_legacy/`.
* **No tests travel with code.** Copied modules are covered by the e2e
  suite, not by ported unit tests.
* **Provenance.** A commit that copies code names its source
  (`from vbc_legacy/<path>@<sha>`); pre-copy history stays reachable with
  `git blame -C -C` / `git log --follow`.
* **Keep phase 1 short.** Two copies of the orchestrator are acceptable only
  while legacy is frozen and the migration is moving.

## Responsibility split

### Server (`vbc serve`)

Copied from legacy into the new tree (see Code strategy): `domain/`,
`config/`, `infrastructure/` (ffmpeg, ffprobe, exif_tool, manifest_watcher,
polling_watcher, gpu_monitor, logging, event_bus), `pipeline/`.

New package `vbc/service/`:

| Module | Responsibility |
|---|---|
| `service/state_store.py` | Subscribes to domain events, maintains the read model (the business half of today's `UIState` + `UIManager` handlers), bumps `rev`, computes rates/ETA anchors (see below). Events arrive synchronously from orchestrator worker threads and from every parsed FFmpeg progress line, so bus callbacks only mutate memory under one lock and notify; no I/O, and never `publish()` while holding the lock. Also holds the latest GPU sample written by `GpuMonitor`. |
| `service/commands.py` | `CommandService`: validates client commands, persists config (`save_dirs_config` moves here), publishes domain events. |
| `service/api.py` | HTTP app (FastAPI + uvicorn — **decided**; same stack as mypowers, OpenAPI for free). Thin: translate HTTP ↔ StateStore/CommandService. |
| `service/bootstrap.py` | Headless composition root, wiring cut out of legacy `compress()`: config, bus, adapters, orchestrator, StateStore, CommandService. Used by `serve` (and by the future local mode, see Open decisions). |
| `vbc/contracts/` | Transport-only Pydantic DTOs with `schema_version`. The only module clients may share. `docs/api/openapi.json` regenerated by a script (`scripts/export-openapi.py`) whenever the API changes; no drift test. |

The container packages only the server and its core modules, with the locked
`server` dependency group. It starts through `python -m vbc.service`; local
`vbc serve` delegates to the same startup function. The image excludes
`vbc.cli`, `vbc/clients/`, legacy and terminal presentation dependencies.
Manifest maintenance keeps its core on the server and its Rich command-line
presentation in `scripts/`. Client-only changes do not trigger image releases.

### Clients

Own only view state: overlay visibility/active tab, dim level, sparkline
preset/palette/metric, logs page index, Dirs editor cursor/input buffer/staged
edits, unicode stripping, every `format_*` helper. Dirs editing becomes "edit
locally, then send the whole ordered list" (`PUT /dirs`).

All clients are Python and live in `vbc/clients/`; they import only
`vbc/contracts/` and a shared thin HTTP client (`clients/api_client.py`,
`httpx`: long-poll loop, clock-skew correction, retries).

`vbc tui` (phase 1): the current Rich dashboard and overlays, fed by the API.
A background thread runs the `/state` long-poll and replaces a local snapshot;
the render loop extrapolates counters from anchors (see below). Keys map to
commands: `<`/`>` → `PUT /threads` with an absolute target computed from the
last snapshot, `R` → `POST /refresh`, no stop/shutdown key (the TUI never
stops the server), `Q` / Ctrl+Q / Ctrl+C → quit the client (does **not**
stop the server; interrupting the server is an explicit command). Overlay
tabs fetch `/config`, `/io`, `/dirs`, `/errors`, `/logs` on demand.

One-shot commands (phase 1): non-interactive commands for scripts and quick
checks, **peers of `serve` and `tui`** in one flat `vbc` command set (no
separate `ctl` group), one per endpoint: `vbc status` (one-shot or
`--watch`), `vbc threads N`, `vbc refresh`, `vbc stop
[--interrupt|--cancel]`, `vbc dirs [list|set …]`, `vbc errors`, `vbc logs
[--follow] [--level]`, `vbc config`. Human-readable output by default,
`--json` prints the DTOs as-is; non-zero exit status on HTTP/connection errors.

`vbc-web` (later): separate process, separate entry point, own port. Serves
static files and proxies `/api/*` to the server (same origin for the
browser, so no CORS configuration on the server). Replaces
`infrastructure/web_server.py` and the `--web` flag; until it exists, the
legacy `vbc run <dirs> --web` keeps working.

## Classification of today's `UIState`

| Server read model | Client view state |
|---|---|
| completed/failed/skipped/hw_cap/cam_skipped/min_ratio_skip/interrupted counts, `*_at_last_discovery`, `session_completed_base` | `show_overlay`, `active_tab`, `overlay_dim_level` |
| files_to_process, already_compressed, ignored_small, ignored_err, total_files_found | `dirs_cursor`, `dirs_input_mode`, `dirs_input_buffer`, `dirs_pending_*`, `dirs_error_msg` |
| total_input_bytes, total_output_bytes, throughput history → **rate + ETA anchors** | `gpu_sparkline_*` selection, `logs_page_index`, `logs_page_size` |
| active_jobs, recent/web_recent jobs, job_start_times | `strip_unicode_display`, `ui_title`, `show_info`, `info_message` |
| pending queue (count + paginated endpoint) | |
| discovery_finished(_time), current_threads, source_folders_count, processing_start_time | |
| shutdown/interrupt requested, error_paused + texts, repair_*, finished, waiting_for_input | |
| last_action (+time), session_error_logs | |
| config_lines, io_* stats, queue sort/seed, log_path, debug, dirs_config_entries | |

## Configuration window (Settings / IO / Dirs tabs)

The split is **data on the server, window on the client**.

Today `main.py:574-627` formats the effective config into display strings
(`ui_state.config_lines`), and `ui/modern_overlays.py:145`
(`parse_config_lines`) parses those strings back into a dict to render them.
Only the server knows the effective values (YAML + local `vbc.yaml` overrides
+ CLI flags + detected encoder), so it must produce them, but as structured data
instead of text:

| Part | Owner | Notes |
|---|---|---|
| Effective config values (threads, prefetch, encoder/preset, quality + dynamic quality, camera filter, metadata method, autorotate rule count, manual rotation, extensions → suffix, queue sort/seed, CPU fallback, min size, skip AV1, verify action, auto repair, clean errors, debug, session start, demo parameters) | Server | `GET /api/v1/config` returns typed fields grouped by section (`general`, `encoder`, `quality`, `filters`, `metadata`, `queue`, `errors`, `demo?`), plus `source` per field (`default`/`global`/`local`/`cli`) where it is known. No preformatted strings. |
| IO data (input dir stats, output/error dirs, suffixes, log path) | Server | `GET /api/v1/io`. |
| Dirs list (ordered paths + enabled) and saving it | Server | `GET /api/v1/dirs`, `PUT /api/v1/dirs` with `If-Match` (see Commands). Validation, `save_dirs_config`, `InputDirsChanged` all server-side. |
| The window itself: tabs, layout, labels, units, size/bitrate formatting, which fields are shown | Client | Each client renders the same JSON its own way (Rich panel, HTML table). |
| Dirs editor interaction: cursor, add-mode input buffer, staged toggles/removes/reorder, inline error | Client | Staged locally, sent as one full ordered list on save; a `412` or `422` from the server is shown inline. |
| `strip_unicode_display` | Client (with server default) | A display preference: the server exposes the configured default in `/config`, each client may override it locally. |

The configuration window stays **read-only** except for Dirs, as today.
Runtime changes that already exist (threads, refresh, shutdown) go through the
command endpoints rather than through the config window. Editing other config
fields over the API is a separate future feature: it needs validation, decisions
about persistence (global vs local YAML) and a rule for whether running jobs are
affected.

Config changes made outside the API (e.g. a manual YAML edit followed by
refresh) bump a `config_rev` that is included in `/state`. Clients refetch
`/config` only when `config_rev` changes.

## Paths and storage roots

Once the server runs elsewhere (another host, container, VM), every path shown
in the Dirs/IO tabs means **a path in the server's filesystem**, not the
client's. Three places depend on this:

* `input_dirs` (e.g. `/srv/producer/metadata/`, `/data/videos`);
* suffix-mode outputs are **siblings** of the input (`/data/videos` →
  `/data/videos_out`, `/data/videos_err`), so mounting only the input directory
  is not enough;
* metadata manifests written by producers contain **absolute**
  `inputs` and `output_path` (`CompressionManifest.validate_paths`), resolved
  by the server as-is.

Recommended approach, in order:

1. **Identity mounts (deployment rule, no code change).** Mount every storage
   root at the *same* path on the server as on the host where producers write
   (`/srv/producer` → `/srv/producer`, `/data/videos` + `videos_out` + `videos_err`, or their common
   parent). Manifests, suffix outputs and existing configs keep working
   unchanged. Alternatively use explicit `output_dirs`/`errors_dirs` inside the
   mounted tree instead of suffix mode.
2. **Storage roots (server config + API).** Optional on loopback; enforced
   when the API is bound to any other address (see Security), where the
   roots are the configured ones plus every media mount of the process
   (image 0.1.2; see the step 8 progress note). Config section, e.g.:

   ```yaml
   storage:
     roots:
       - {name: producer, path: /srv/producer}
       - {name: videos, path: /data/videos}
   ```

   * `PUT /api/v1/dirs` rejects (`422`) any path, and its derived output/error
     dir, that is not inside a declared root. Without this, any API client
     could make the server read and write anywhere it has permissions.
   * `GET /api/v1/roots` and `GET /api/v1/fs?root=&path=` (directory names only, never file contents,
     no files, no symlinks escaping the root) let clients **pick** a directory
     on the server instead of typing a path that only makes sense on the
     client.
   * `/io` and `/dirs` report per-directory `exists`/`writable`, so a missing
     mount is visible in the window instead of failing at the first job.
3. **Path mapping (only if identity mounts are impossible).** Optional
   `storage.path_map: {"/srv/producer": "/data/producer"}` applied to manifest `inputs` and
   `output_path` on load (and reversed in anything reported back to the
   producer). Kept as a fallback because it adds a second set of names for the
   same files.

## Logs

Two different things are called "logs" today, and both become server data:

| | Today | API |
|---|---|---|
| **Session error log** (Logs tab) | `SessionErrorEntry` list in `UIState` (`ui/state.py:15`): path, size, width/height, fps, codec, audio codec, duration, `error_message`, `created_at`; in memory only, lost on exit | `GET /api/v1/errors?since=<id>&limit=` |
| **Application log** | `logging.FileHandler(log_file, mode="w")` in `infrastructure/logging.py:64`; overwritten on every start; path shown in Settings | `GET /api/v1/logs?since=<seq>&limit=&level=` |

Session error log (`/errors`):

* Entries get a monotonic `id`; clients ask `since=<last id>` and append.
  `/state` carries `errors_count` and `errors_last_id`, so clients know when to
  fetch more without polling `/errors` separately.
* Structured, not preformatted: the TUI line
  `name • 712.5MB • 640x1280 • 25fps • h264 • 21m 37s` and the red error line
  are rendered by the client from fields.
* Add fields the server already knows but the UI drops today:
  `stage` (`discovery` / `manifest_preflight` / `encode` / `verification` /
  `repair`), `exit_code` (for `ffmpeg exited with code …`), `manifest_request_id`
  when the job came from a manifest, `job_id`, and the **full** error message.
  Truncation (`…` in the TUI) is the client's job.
* Bounded in memory (e.g. last 1000 entries; `410` if `since` is older than the
  buffer, so the client refetches from the start). Optional persistence
  (JSONL next to the log file) if errors must survive a server restart; the
  `.err` markers on disk remain the durable per-file record either way.

Application log (`/logs`):

* An in-memory ring buffer handler attached to the root logger, next to the
  existing file handler: `seq`, `ts`, `level`, `logger`, `message`, optional
  `job_id`. Same `since` paging as `/errors`.
* `PUT /api/v1/runtime/log-level {"level": "DEBUG"}` replaces restarting with
  `--debug` (pattern from mypowers).
* `vbc serve` logs to stdout/stderr (the container convention); the log
  file becomes optional (`--log-file`), kept for local runs. Switching
  `mode="w"` to append/rotation for that file is an independent change.
* Live tail uses the same long-poll mechanism (`wait=` parameter); no separate
  streaming protocol.

## API (v1)

All responses carry `schema_version`, `rev` and `server_time` (epoch ms, used by
clients to correct clock skew).

Read:

| Endpoint | Content |
|---|---|
| `GET /api/v1/state?since=<rev>&wait=<s>` | Main snapshot (run status, counters, bytes, throughput anchors, active jobs with progress anchors, last N recent jobs, queue count, `stop_requested`, `config_rev`, `errors_last_id`). Long-poll: returns immediately if `rev > since`, otherwise holds up to `wait` (≤30 s) and returns `204 No Content` on timeout (the client simply polls again). Plain polling uses `ETag: "<rev>"` and returns `304` only for a request carrying `If-None-Match`. |
| `GET /api/v1/gpu?since=<seq>` | Latest GPU sample (`gpu_data` today) plus samples newer than `seq` from a bounded history (for the TUI sparkline); `enabled: false` when monitoring is off. Anchors are deferred (see Open decisions). |
| `GET /api/v1/queue?offset&limit` | Pending files (can be 10k+, never inside the snapshot). |
| `GET /api/v1/jobs/recent?limit` | Longer activity history (bounded ring, e.g. 500). |
| `GET /api/v1/errors?since&limit` | Session error log entries (see Logs). |
| `GET /api/v1/logs?since&limit&level&wait` | Application log ring buffer (see Logs). |
| `GET /api/v1/config` | Effective config (today's Settings tab lines, structured). |
| `GET /api/v1/io` | Input dir stats, output/error dirs, queue sort/seed, log path. |
| `GET /api/v1/dirs` | Ordered `[{path, enabled}]` + `dirs_rev`. |
| `GET /health/live`, `GET /health/ready` | Liveness / readiness in the HomeStack format (`application/health+json`, heartbeat checks; see Deployment: HomeStack). |
| `GET /.well-known/homestack/status` | HomeStack telemetry schema 3: `status` + `process_rss_bytes`. Never routed publicly. |

Commands (absolute and idempotent where possible):

| Endpoint | Maps to |
|---|---|
| `PUT /api/v1/threads {"target": n}` | New `SetThreadCount` event (replaces client-side ±1). The orchestrator clamps and the response returns the **effective** value; `threads_min`/`threads_max` are exposed in `/config` (today `1..8` hard-coded, to be moved to config). |
| `POST /api/v1/refresh` | `RefreshRequested`. This is also the **only** way to resume after `paused_on_error` or `waiting_for_input`: it always triggers a full rediscovery; there is no "continue without rescan". |
| `POST /api/v1/stop {"mode": "graceful"\|"interrupt"}` | **Stops processing, never the server** (decided: the container runs non-stop). Absolute `SetShutdownRequested(true)` / `InterruptRequested`; repeating it is a no-op. Running jobs finish (graceful) or are interrupted, the server goes to `stopped` and only an explicit `POST /refresh` (TUI key R) resumes in the same process; watcher and Dirs-change refreshes are ignored while stopped (`RefreshRequested.automatic`). A stop does not survive a restart. TUI key S sends a graceful stop, S again while stopping cancels it (use before a container update). |
| `DELETE /api/v1/stop` | `SetShutdownRequested(false)`: withdraws a pending graceful stop. |
| `PUT /api/v1/runtime/log-level {"level": …}` | Adjust root logger level at runtime |
| `PUT /api/v1/dirs` + `If-Match: <dirs_rev>` | Validate → `save_dirs_config` → `InputDirsChanged` + `RefreshRequested`; `412` on stale rev |

Run status in `/state` is one field with a reason, so clients know which
actions to offer: `discovering`, `running`, `paused_on_error`,
`waiting_for_input` (`wait_on_finish`), `verification_abort`,
`shutting_down`, `interrupted`, `idle` (see Process model).

Security: the listen address is configurable (`vbc serve --host <ip>
--port <n>`, or `server.host` / `server.port` in config; `0.0.0.0` or a
specific interface IP for access from other hosts) and defaults to
`127.0.0.1`.

**No authentication, no TLS** (decided). The API is plain HTTP for a trusted
network:

* On loopback a token adds nothing; on a remote link without TLS a token
  travels in clear text with every request, so it would be protection in
  name only. Whoever can reach the port can use the API.
* Kept: a Host/Origin allowlist, which stops web pages open in a local
  browser from driving the API (CSRF, DNS rebinding) — the realistic threat
  on loopback.
* When bound to anything other than loopback, `storage.roots` (see Paths) is
  **mandatory** and the server refuses to start without it, so that anyone on
  the network can at most point the server at directories inside the
  declared roots via `PUT /dirs`.
* **Threat model (why this is enough).** The API is a control plane only:
  a caller can make the server work or stop, change threads, add / remove /
  enable folders inside `storage.roots`, and read status. It cannot read,
  download, upload or delete media, and cannot change encoding or source
  policies. The worst disclosure is the file list and its metadata (names,
  sizes, codecs, server paths, error messages); file names are typically a
  date and a counter. The worst disruption is stopping or pausing work.
* **Source policies stay server-side.** Files are deleted or moved only by
  policies the API cannot change: `general.source_policy`
  (`delete_after_success`), per-folder `vbc.yaml` overrides, producer
  manifests (`delete_after_success` / `move_after_success` / `move_all`) and
  `metadata.audio_only: delete`. Adding a folder through the API applies the
  policy that folder already has (default `keep`).
* **Access paths.** Desktop: `vbc tui` and the one-shot commands over plain
  HTTP in the LAN. From outside (phone, other networks): the web client
  later, published through Pangolin, which provides HTTPS and its own
  password; `/.well-known/homestack/status` is never routed publicly. No
  application-level tokens in either path.
* Exposing the server beyond a trusted network is the deployment's job
  (firewall, SSH tunnel `ssh -L`, WireGuard/Tailscale), not VBC's. Built-in
  TLS + token can be added later as a separate feature if ever needed.

## Changing counters without streaming every tick

Principle: **the server sends anchors, clients extrapolate** (dead reckoning).
The server publishes a new `rev` only when the prediction stops being true, not
on every FFmpeg progress line.

Active job in the snapshot:

```json
{
  "id": "a1b2",
  "name": "DJI_0042.MP4",
  "started_at": 1762250000000,
  "input_bytes": 4831838208,
  "quality": "CQ45",
  "progress": { "percent": 41.7, "at": 1762250123400, "rate": 0.183 }
}
```

Global part:

```json
{
  "throughput": { "bytes_per_s": 61200000, "at": 1762250123400 },
  "eta_at": 1762252710000,
  "processing_started_at": 1762249000000
}
```

Client, at its own frame rate (1–10 Hz), with no network traffic:

```
now      = local_now + (server_time - local_time_at_receive)
percent  = min(p.percent + p.rate * (now - p.at)/1000, 99.5)
elapsed  = now - started_at          # job and session timers
eta_left = max(eta_at - now, 0)      # countdown
```

Server, inside `StateStore` on each `JobProgressUpdated`:

* update an EWMA of `rate` (%/s) per job;
* compute what a client currently predicts from the last published anchor;
* bump `rev` only if `|predicted - actual| > 2 p.p.`, or the rate changed enough
  to drift more than 2 p.p. within 10 s (`|Δrate| × 10 s > 2 p.p.`; a relative
  "20 %" rule re-anchored on every noisy sample at low speeds), or 30 s passed
  since the last anchor (cap on staleness). Measured with a simulated clock:
  a 3-minute encode with 360 progress lines produces 7–12 snapshots and the
  client's extrapolation stays within ~2 p.p.;
* always bump on discrete changes: job start/finish/fail, counter changes,
  status changes, queue count, thread count, dirs.

Result: with 8 parallel jobs, a few snapshots per minute instead of dozens per
second, while the timers and progress bars still move smoothly. If the
connection drops, the client keeps extrapolating, greys out after ~15 s without
a response and resynchronises from the next full snapshot. There is no replay
log, so no stateful catch-up logic is needed.

Snapshot size stays small (active jobs ≤ thread count, recent jobs capped at
~20, queue reduced to a count), so sending the whole snapshot on every `rev` is
simpler than diffs. JSON Patch or diffs are a later optimisation, needed only
if measurements demand it.

### Why HTTP long-poll and not WebSocket

Plain HTTP + JSON is enough for this traffic profile and works unchanged through
any reverse proxy, across hosts/VMs, from `curl` and from scripts. (For
reference: mypowers *does* expose WebSocket streams at `/api/v1/events`, and its
Rust TUI uses `tokio-tungstenite`. Both models work across nodes, because a
WebSocket is an HTTP upgrade.) Long-poll on `rev` gives near-immediate updates
without a second protocol. SSE can be added later on top of the same `rev`
mechanism without changing the read model.

## Deployment: HomeStack

Final target: VBC is deployed by HomeStack exactly like the owner's other
services, following the `homestack-service` skill
(`~/DEV/homestack/.agents/skills/homestack-service/`). Target topology:
`vbc serve` runs in the HomeStack container, `vbc tui` and the one-shot
commands stay on the desktop and reach it by its DNS name. Phase 1 runs both
on one machine against local folders; the container is the next stage.

Input folders: phase 1 keeps today's Dirs editing (type a path, enable /
disable, remove, reorder), saved through `PUT /dirs`. Later: picking a folder
from the server's tree under `storage.roots` (`/fs`) instead of typing it,
and folders provided by HomeStack external mounts.

What the HomeStack contract requires from VBC:

| Area | Requirement | Consequence for this plan |
|---|---|---|
| Container | One `linux/amd64` OCI image, `ENTRYPOINT` = `vbc serve`, unprivileged LXC, disposable root | Containerfile in the repo; base chosen per the skill's image preflight (Python + ffmpeg + exiftool: Debian slim rather than Alpine/musl unless proven to work). |
| Network | Plain HTTP inside the LAN; Pangolin terminates public HTTPS and remote auth; clients use the DNS name `<hostname>.<domain>`, never an IP | Matches the no-token/no-TLS decision. In the container `vbc serve` binds `0.0.0.0` (env, e.g. `VBC_SERVER_HOST`). The Host/Origin allowlist must be **configurable** and include the service DNS name and, if exposed, the Pangolin subdomain. `exposure:` empty at first (LAN only). |
| Health | `/health/live` and `/health/ready` in `application/health+json` (draft-inadarei); heartbeat every `HOMESTACK_HEARTBEAT_SECONDS` (default 60) through the main serving path; both return `503` after three missed beats; checks `heartbeat:age`, `heartbeat:count` | Heartbeat = a request to the server's own listener on a light route that reads `StateStore` (same workers and lock as `/state`). `/health/ready` adds dependency checks: `ffmpeg`/`ffprobe`/`exiftool` executable, every storage root mounted and writable, orchestrator thread alive. `paused_on_error` or `idle` is a business state and stays ready. |
| Telemetry | `GET /.well-known/homestack/status` → `{"schema":3,"status":…,"process_rss_bytes":…}`, `VmRSS` read per request, no sampler | Small handler in `service/api.py`; RSS covers the server process only (FFmpeg children are measured by the PVE agent at container level). |
| Persistence | Exactly one volume at `/home/app` (`persistence.mount`); everything durable lives there | VBC config incl. the Dirs list written by `PUT /dirs` → `/home/app/config/vbc.yaml`; optional session-error JSONL → `/home/app/state/`. The image keeps `/home/app` a real empty dir and refuses to start when it is not mounted. `state_model: local`, `instances: 1`, `placement: single`. |
| Media storage | HomeStack today attaches only `mp0`; its update review refuses extra/bind mounts. **External mounts are planned in HomeStack** (owner: simple to add later) | The media trees are mounted by HomeStack as external mounts; the identity-mount rule from Paths applies to them, and `storage.roots` lists exactly those mount points. Until the feature exists, the container can be tested only with media on a test volume. Not a blocker for steps 1–7. |
| Settings | Operator-selected values via schema-3 `inputs.yaml` (environment delivery); no secrets or host-specific values in repo/image/manifest | Env overrides for host/port, allowed hosts, storage roots; the rest from the config file on `/home/app`. |
| Stop / update | Image replacement and container stop send SIGTERM; root is replaced | SIGTERM → graceful shutdown with a bounded deadline (`server.stop_timeout`), then `InterruptRequested`, so a long encode cannot outlive the Proxmox stop timeout. Interrupted jobs must leave no half-written output that a restart treats as done. |
| Resources | Cores, memory, swap, rootfs measured on the built image under a real encode workload, recorded next to the runtime contract | Measured in step 8, not guessed. |
| Manifest | Schema 3: `.homestack/services/vbc/{service,defaults,inputs}.yaml`, `runtime.telemetry: homestack`, healthcheck `http` on `/health/ready` expecting `200` | Written in step 8. |
| Release | Annotated tags `vbc-X.Y.Z` from `scripts/tag-image.py` (clean pushed HEAD, `--dry-run`), CI publishes `ghcr.io/hipotures/vbc:X.Y.Z` (public, immutable), manual `dev-<commit>` builds, ordinary pushes only run checks | CI workflow + tagging script in step 8; the "does this change the image?" release rule recorded in `AGENTS.md`. |
| GPU | Schema 3 `runtime.devices` (`/dev/nvidia*`, `/dev/dri/*`), attached manually in Proxmox first; node-local | Out of scope for now; CPU encoding works without devices. NVENC/VAAPI in the container needs matching userspace libraries in the image. |

## Process model

| Command | What runs |
|---|---|
| `vbc serve [--config] [--host] [--port]` | `bootstrap` + API, headless, no Rich, no keyboard. Input dirs from config (positional args optional). `--demo` uses `DemoOrchestrator`. |
| `vbc tui [--api http://host:port]` | TUI client only (phase 1). |
| `vbc status`, `vbc threads N`, `vbc refresh`, `vbc stop`, `vbc dirs`, `vbc errors`, `vbc logs`, `vbc config` | One-shot API commands (phase 1). `--api` on every client command, also from env (`VBC_API`) or client config; default `http://127.0.0.1:<default port>`. |
| `vbc run <dirs>` | Today's `vbc <dirs>`. During migration it runs `vbc_legacy`; after legacy is deleted it becomes the launcher (see Open decisions). |
| `vbc-web --api …` | Web client only (later). |

All of these are **peer subcommands of one Typer app** (`vbc/cli.py`).
A bare `vbc <dirs>` no longer works, because a directory named like a
subcommand (`./serve`) would be ambiguous; when the first argument is not a
known subcommand, `vbc` prints `use: vbc run <dirs>` and exits non-zero.

`vbc serve` lifecycle:

* **The process outlives the queue.** When processing finishes, the server
  goes to `idle` (keeping state, errors and logs readable) instead of exiting
  as the CLI does today; `POST /refresh` starts a new pass, `input_dirs.watch`
  starts one automatically. `POST /stop` ends a pass early (status
  `stopped`, refresh resumes). The process ends **only on a signal**; there is
  no API to end it.
* **Threads and signals.** uvicorn owns the main thread and its event loop;
  `bootstrap` returns an object with `start()` / `stop()` that runs the
  orchestrator in its own thread. uvicorn installs its own SIGINT/SIGTERM
  handlers, so the server hooks its shutdown: SIGTERM → `SetShutdownRequested`
  (graceful), a second SIGINT → `InterruptRequested`; uvicorn stops after the
  orchestrator thread has joined.

## Behaviours that must be preserved

Critical requirements carried over from legacy unchanged. The e2e suite checks
them.

* **A file already encoded by VBC is moved into the output tree, never left
  in the input tree and never sent to the error tree.** Detected by the
  `VBCEncoder` tag (XMP or format tag), it is moved unchanged to
  `<input>_out/` (same relative path). Reason: the owner keeps sources after
  compression and treats "nothing left to compress" as "every source in the
  input tree has a compressed version and may be deleted". A VBC output left
  in the input tree would be deleted as if it were a source with a backup; one
  in the error tree would look like a failure.

## Testing strategy

Decided: **no unit tests; a small end-to-end suite only.** Today's suite
(556 fast tests, an early-project test architecture) is deleted in step 1,
and no unit tests are written for the new tree. Reason: as the software
grows, keeping fine-grained tests green costs more than the code itself
(well over half of the maintenance effort), while the behaviour that matters
is visible from outside.

* **Black box.** Tests start real processes (`vbc serve` on a free loopback
  port with a temporary config, the one-shot commands with `--json`, real
  FFmpeg / FFprobe / ExifTool) and assert only externally visible results:
  files on disk (outputs, `.err` markers, moved errors), `/state` counters,
  `/errors` entries with their `stage`, HTTP status codes. No imports of
  internals, no mocks.
* **Fixtures are generated, not committed.** A session fixture builds a tiny
  media set with FFmpeg (`testsrc`, 1–3 s): normal clips, an already
  compressed one, one below `min_size`, a corrupt file, one with rotation
  metadata, one manifest. Seconds to create, nothing binary in git.
* **Scenarios (small, stable):** full pass to `idle`; Dirs add / disable /
  remove with `If-Match` (and the `412` on a stale rev); threads up/down
  with clamping; graceful shutdown, cancel, interrupt; pause on error →
  refresh; long-poll returns on change and `204` on timeout; health and
  telemetry (heartbeat with a short `HOMESTACK_HEARTBEAT_SECONDS`, stalled
  path → `503`). `vbc tui`: one smoke run in a pty against `serve --demo`
  (starts, renders, quits on Ctrl+C without stopping the server).
* **Legacy as oracle.** Step 2 records what legacy `vbc run` produces on the
  same fixture set (output file names, markers, error files, final counts)
  as an expected-results file. The new server must reproduce it. This
  comparison disappears with legacy.
* **When.** The expected-results file in step 2; the e2e suite grows from
  step 5 (first scenario: full pass) and is complete at the end of phase 1.
  Step 8 reruns the health/telemetry scenarios against the built image.
* **Budget.** The whole suite runs in a few minutes; a scenario that keeps
  breaking for reasons other than real regressions is simplified or deleted
  rather than nursed.

## Working rule: restart over workaround

This document is the source of truth; the code follows it. Implementation is
allowed to fail and be repeated:

* **Baseline.** The commit that finishes this plan, before step 1, is tagged
  (e.g. `service-split-baseline`). Every step is a small commit on top of it,
  and legacy keeps `vbc run <dirs>` working throughout, so going back costs
  only the discarded steps.
* **When the plan turns out wrong**, i.e. implementation shows that an
  architectural decision was wrong or missing (a contract, a layer
  boundary, the data model, the process model, something several later
  steps depend on): **stop, do not work around it.** Record what was learned
  in this document, fix the decision, and implement again from the baseline
  (or from the last step that is still valid).
* **How to go back without rewriting history.** No force-push and no moving
  of tags: the next attempt starts on a fresh branch from the baseline (or
  from the last good step); the failed attempt stays as a reference branch
  until it is no longer useful.
* **What does not need a restart:** a local bug, a missed edge case or a
  refactor inside one module; those are fixed in place.
* Restarting is cheaper than carrying a wrong decision forward: forcing the
  code down a path we do not want complicates every later step.

## Migration steps

Progress: baseline tag `service-split-baseline`; **steps 1–7 done — phase 1
complete**; **step 8 (HomeStack packaging) in progress**: image
`deploy/container/` on `debian:testing-slim` (FFmpeg 9.0.2 with NVENC; Debian
trixie's 7.1 lacks `-filter_buffered_frames`, Alpine has no NVENC/glibc,
Ubuntu 26.10 has 8.1.2 — owner chose testing to match the dev host), schema-3
manifest `.homestack/services/vbc/`, `scripts/tag-image.py`,
`scripts/image-admission.sh`, `.github/workflows/image.yml`; image e2e
passes. Published 2026-11-08: manifest on `main` (`.homestack/services/vbc/`,
commit 7c220ba; HomeStack reads manifests only from the default branch),
image `ghcr.io/hipotures/vbc:0.1.0` from tag `vbc-0.1.0` (abe408a), pullable
anonymously; the image e2e passes on the published image. Image **0.1.1**
(tag `vbc-0.1.1`, 9dd0ddd): container port **8080** (HomeStack convention;
`VBC_SERVER_PORT` overrides the port of an existing config, local `vbc serve`
keeps 8770), on-volume storage roots created at start, `<hostname>.<domain>`
accepted without configuration; manifest on `main` switched to 8080 only
after 0.1.1 was published (dff76d8). Image 0.1.2 (owner's first deployment,
Proxmox LXC): nvtop, NVIDIA toolkit env, NVENC
probe with CPU fallback; **storage roots = configured roots + every media
mount** (`vbc/service/storage.py`, read on each use), so `VBC_STORAGE_ROOTS`
is optional and no restart is needed; **mount scan** (at start, `R` in the
Dirs tab, `vbc dirs scan`, `POST /api/v1/dirs/scan`) offers folders directly
inside each mount that hold videos or manifests as *disabled* entries, each
once (`storage.discovered`); a folder's `metadata`/`watch`/`watch_mode` are
detected when the request leaves them out (manifests inside, NFS → polling)
and a dirs change rebuilds the watchers and the orchestrator's entries
without a restart. HomeStack itself was fixed to let new image ENV defaults
replace unchanged old ones (port 8770 had survived the 0.1.1 update). Open: GPU in the
LXC (devices + host driver libraries), real deployment test on Proxmox,
HomeStack external mounts for media. While the migration stays on this
branch, a manifest change must also be copied to `main`. Post-phase-1 fixes from the owner's first run: long-polls are
released when the server stops (no 3 s hang / traceback); `/shutdown` became
`/stop` (processing only, server keeps running, refresh resumes —
`reset_after_stop()` in both orchestrators, statuses `stopping`/`stopped`,
`shutting_down` only for a signal); TUI: no `S` action, `Q` / Ctrl+Q /
Ctrl+C all quit the client at once (a "quit after the running jobs" mode was
tried and dropped: the server finishes them anyway; IXON is disabled so
Ctrl+Q arrives); the server logs TUI sessions (`X-VBC-Client-*` headers,
`POST /api/v1/clients/goodbye` on quit, 20 s silence = vanished);
connection errors as a short `DISCONNECTED` in the header, the reason in the
footer and in `~/.local/state/vbc/tui.log` (server, one-shot commands, TUI client; next: step 8 HomeStack
packaging).
Step 7 notes: `vbc/clients/tui/` = the legacy renderers copied verbatim
(`dashboard`, `modern_overlays`, `gpu_sparkline`, `display_text`,
`keyboard`; commit c89b81b) plus `state.py` (`ClientState`: legacy `UIState`
surface, business fields filled by `apply_*` from the API, active-job
progress extrapolated from anchors on every read, Dirs existence from the
server's flags instead of the client's filesystem), `controller.py` (legacy
`UIManager` view handlers unchanged; keys `<`/`>` → `PUT /threads` absolute,
`R` → refresh, `S` → shutdown or cancel, Ctrl+C → detach, Dirs apply →
`PUT /dirs` with `If-Match`, 412/422 shown inline; API calls on a worker
thread), `poller.py` (long-poll `/state`, refetch `/queue` page, `/errors`
since last id, `/dirs`+`/io` on `dirs_rev`, `/config` on `config_rev`, `/gpu`
every 5 s, DISCONNECTED + automatic resync), `configview.py` (Settings lines
from `/config`, formerly built in legacy `main.py`), `views.py` (DTO-backed
job/file/error views with the shape the renderers expect, `JobStatus`
mirror). Dashboard changes: throughput/ETA and queue total from the server,
DISCONNECTED status, hint line "R = rescan │ S = stop server │ Ctrl+C =
detach". DTOs gained `MediaInfo`, `quality_value`, `rotation_angle`,
`config_source` (active and recent jobs). Parity check in a pty: legacy
`vbc run --demo` and `serve --demo` + `vbc tui` with the same keys render the
same panels and tabs (only the exit code differs: legacy Ctrl+C interrupts,
the client detaches). Not carried over: the legacy demo's mock IO folders
(the demo server shows its real `input_dirs`) and the "No files to process"
info box at the end of a legacy run.
Step 6 notes: `vbc/clients/api_client.py` (httpx, typed DTOs from
`vbc.contracts`, `ApiUnavailable` / `ApiError` with the server's `Problem`,
clock offset from `server_time`, `extrapolated_percent`), `vbc/clients/
commands.py` + `format.py`, and the peer subcommands in `vbc/cli.py`:
`status [--watch]`, `threads N`, `refresh`, `shutdown [--interrupt|--cancel]`,
`log-level LEVEL`, `errors [--since]`, `logs [--since] [--level] [--follow]`,
`config`, `dirs [add|remove|enable|disable|set]` (read, edit locally, `PUT`
the whole list with `If-Match`; `--api`/`--json` go before the `dirs`
subcommand). Exit codes 0 / 1 rejected / 2 usage / 3 unreachable. A client
config file is not needed yet: `--api` or `VBC_API`.
Step 5 notes: `vbc serve` = `vbc/service/bootstrap.py` (`VbcServer`: config +
env overrides `VBC_SERVER_HOST/PORT`, `VBC_ALLOWED_HOSTS`,
`VBC_STORAGE_ROOTS`; off loopback the roots are `storage.roots` plus every media mount (0.1.2);
logs to stdout + `/logs` ring buffer + optional `--log-file`; orchestrator in
its own thread with `wait_on_finish` forced on) and `vbc/service/api.py`
(FastAPI app, uvicorn with VBC's own signal handling: first SIGTERM/SIGINT =
graceful stop with `server.stop_timeout_s`, second SIGINT = interrupt, exit
code 0). Config sections `server` (host 127.0.0.1, port 8770, allowed_hosts,
stop_timeout_s) and `storage.roots`; legacy ignores them. Host/Origin policy:
IP literals and own/loopback names always pass, other names only from
`server.allowed_hosts`. **`idle` = the orchestrator's `WaitingForInput`**:
`ProcessingFinished` is published *before* the auto-repair / error-move step,
so using it for `idle` let a client see an idle server while it was still
moving a failed file (caught by the first e2e scenario). OpenAPI:
`scripts/export-openapi.py` → `docs/api/openapi.json`. E2E suite
(`tests/e2e/test_serve.py`, ~20 s) also runs in CI.
Step 4 notes: `vbc/contracts/` (state, queue, recent jobs, errors, GPU, dirs,
command bodies, `Problem`), `vbc/service/state_store.py` and
`vbc/service/commands.py`. Of the 35 legacy `UIManager` subscriptions, the 18
business ones are ported to `StateStore`; the rest (overlay tabs, dim level,
sparkline preset/palette/metric, logs page, Dirs editor keystrokes) are client
view state for step 7. Checked by running legacy core + `UIManager`/`UIState`
and the new core + `StateStore` on the e2e media, two passes: every business
field is identical (counters, discovery, bytes, errors, repair, queue, recent
jobs). The orchestrator now publishes `ThreadCountChanged` with the clamped
value. `put_dirs`: 428 without `If-Match`, 412 on a stale `dirs_rev`, 422
with per-entry errors (absolute path, length, duplicates, storage roots incl.
derived `_out`/`_err`, existence/readability of enabled dirs). Left for step 5:
config/IO/logs DTOs with their endpoints, per-directory IO stats (legacy
`io_input_dir_stats`).
Step 3 notes: the copied core (`vbc/`, no Rich, no UI imports) run headless
on the e2e media reproduces `expected/legacy_run.json` exactly (both passes).
`job_id` lives on `VideoFile` (set when a worker starts an attempt) and
`CompressionJob.job_id` reads it, so preflight, failure and completion events
of one attempt share it; events outside an attempt (an ffprobe failure during
discovery) use a stable `path:<source>` id. New config field
`general.max_threads` (default 8; effective bound `max(max_threads,
threads)`, also the worker pool size) exists only in the new tree, so it is
not added to `conf/vbc.yaml.example` while legacy reads the same file.
`repair.process_repairs` reports progress through an optional `on_progress`
callback instead of Rich; the input-dir status icons/lines (Rich markup) are
left for `clients/tui/` in step 7.
Step 2 artefacts: `tests/e2e/media.py` (fixture generator),
`tests/e2e/record_legacy.py` (two legacy passes, CPU) and
`tests/e2e/expected/legacy_run.json`; the recording is deterministic (two
independent runs identical apart from tool versions). Legacy behaviours it
pins down: outputs are `.mp4` under `<input>_out/` mirroring subdirectories;
autorotate rules rotate the picture (checked by a 4×4 first-frame
fingerprint); files below `min_size_bytes` and non-video files are left
untouched; a corrupt file is moved to `<input>_err/` with a `.err` marker
("File is corrupted (ffprobe failed to read). Skipped."); a file already
tagged `XMP-xmp:VBCEncoder` is **moved** unchanged into `<input>_out/`; a
manifest produces its `output_path` and is moved to
`<meta>_out/<date partition>/`; a second pass changes nothing.

Every step leaves `vbc run <dirs>` (legacy) working, `mkdocs build --strict`
green and, once it exists, the e2e suite green.

1. **Move to legacy.** `git mv vbc vbc_legacy`, `git rm -r tests/` (unit and
   integration suites), rewrite imports `vbc.` → `vbc_legacy.`; new
   `vbc/cli.py` with only `vbc run <dirs>` delegating to
   `vbc_legacy.main`. Also in this commit:
   * `pyproject.toml`: `[tool.hatch.build.targets.wheel] packages` gets
     `vbc_legacy` next to `vbc`, entry point `vbc = "vbc.cli:app"`;
   * `scripts/*.py` that import `vbc` (5 today) switch to `vbc_legacy`;
   * `docs/api/*.md` mkdocstrings references (`::: vbc.…`, 38 today) point at
     `vbc_legacy` so `mkdocs build --strict` stays green; they move back to
     `vbc` module by module as the new tree is written;
   * `AGENTS.md` / `CLAUDE.md` and `docs/development/`: the legacy rules
     (frozen, no cross imports, deleted at the end) and the testing strategy
     (e2e only; drop the unit/integration commands and `pytest-cov` /
     `pytest-mock`).

   Mechanical commit: run the legacy suite once *before* deleting it (556
   passed at plan time) and check `vbc run --demo` by hand after the move.
2. **Legacy expected results.** Fixture generator (see Testing strategy) +
   one recorded legacy `vbc run` over it: output names, markers, error
   files, final counts, saved as the expected-results file the new server
   must reproduce.
3. **Copy the core into `vbc/`, fixing leaks in the copy.** `domain/`,
   `config/`, `infrastructure/` adapters and watchers, `event_bus`,
   `logging`, `pipeline/`, then:
   * `pipeline/repair.py`, `pipeline/error_file_mover.py`: Rich output →
     events/logging; `config/input_dirs.py`: plain text, no Rich markup.
   * Drop the `Dirs*` events from `domain/events.py` (they belong to the TUI
     client).
   * Replace `ThreadControlEvent(change)` with `SetThreadCount(target)`;
     move the `1..8` limit to config.
   * Replace the `RequestShutdown` toggle with `SetShutdownRequested(bool)`.
   * Introduce `job_id`: assigned when a job starts and carried by every
     `Job*` event (and by session error entries).
   * `GpuMonitor` writes to a small sink interface instead of `UIState`.
4. **`StateStore`, `CommandService`, contracts, anchors.** Read model built
   from handlers cut out of legacy `UIManager`; Dirs saving cut out of
   `on_dirs_apply_changes`; `vbc/contracts/` DTOs, `rev`, rate EWMA,
   dead-reckoning thresholds, ETA anchor, GPU sample history. Handlers are
   carried over from all `UIManager` subscriptions (35 today), not only the
   counters; checked end to end from step 5.
5. **API + `vbc serve`.** `service/bootstrap.py` (wiring cut out of legacy
   `compress()`); FastAPI app with the endpoints above, long-poll via
   `asyncio.Event` set with `loop.call_soon_threadsafe` from the bus
   callback; configurable Host/Origin allowlist, health with heartbeat and
   HomeStack telemetry endpoint (already in the HomeStack format), stdout
   logging, OpenAPI export script; lifecycle and signals as in Process
   model. First e2e scenario: full pass over the fixtures matches the
   step-2 expected results.
6. **One-shot commands.** `clients/api_client.py` + `vbc status`/`threads`/
   `refresh`/`shutdown`/`dirs`/`errors`/`logs`/`config`. Comes before the
   TUI because it is small and exercises every endpoint end to end
   (the e2e scenarios drive the server through these commands).
7. **`vbc tui`.** Panels, overlays and `format_*` helpers copied from
   `vbc_legacy/ui/` into `clients/tui/`, data source switched from `UIState`
   to the client-side snapshot (DTOs + client view state); long-poll thread,
   local extrapolation ticker, key → command mapping. Parity check: the same
   demo session in legacy `vbc run --demo` and in `serve --demo` + `vbc tui`
   shows the same panels. Phase 1 ends here (docs for `serve`, `tui`, `run` and the
   one-shot commands
   land with it).
8. **HomeStack packaging.** Containerfile, entrypoint, `/home/app` layout,
   heartbeat + health/telemetry endpoints verified on the built image (short
   `HOMESTACK_HEARTBEAT_SECONDS`, stalled main path → `503`), resource
   measurement under a real encode, schema-3 manifest, tagging script and
   release CI. Real deployment with media waits for HomeStack external
   mounts.
9. **Web client (later).** `frontends/web/` (own entry point `vbc-web`):
   static page + small JS client (long-poll loop + local ticker for
   extrapolation) + proxy, reaching parity with the legacy HTMX dashboard.
10. **Delete legacy.** Once `vbc run <dirs>` has its replacement (see Open
   decisions) and the web client exists: remove `vbc_legacy/` and the legacy
   expected-results comparison in one commit. If the web client is still far off, legacy
   may be deleted after step 8 and `--web` dropped until step 9 lands.
11. **Docs.** `docs/architecture/` (this page → final design), API reference,
    user guide for `serve`/`tui`/`run`/one-shot commands (phase 1), HomeStack
    runtime contract (step 8) and `vbc-web` (later);
    README dependency notes (FastAPI, uvicorn, httpx).

## Decisions

* Language: **Python stays** (decided) for the server and for the clients
  (`vbc tui`, the one-shot commands, `vbc-web`). No Rust rewrite; mypowers is referenced
  only for its API conventions, not its implementation stack.
* Security (decided): plain HTTP, no token, no TLS; trusted network only,
  Host/Origin allowlist, storage roots enforced off loopback (configured + media mounts).
* Stop vs. shutdown (decided 2026-11-08): the API and `vbc stop` stop
  *processing* only; the server process (a HomeStack container running
  non-stop) ends only on a signal. The TUI has no stop/shutdown action at all:
  `Q`, Ctrl+Q and Ctrl+C quit it at once.
* Command line (decided): one flat set of peer subcommands — `vbc serve`,
  `vbc tui`, `vbc run <dirs>`, `vbc status`, `vbc threads`, `vbc refresh`,
  `vbc stop`, `vbc dirs`, `vbc errors`, `vbc logs`, `vbc config`. No
  `ctl` group.
* Delivery order: **server + one-shot commands + `vbc tui` first** (phase 1, steps
  1–7), HomeStack packaging (step 8), web client later (step 9), legacy
  deleted in step 10.
* Code strategy: **legacy package + new tree** (decided): today's code moves
  to `vbc_legacy/`, the new `vbc/` takes over only what it needs, legacy is
  deleted at the end.
* Deployment target: **HomeStack** (decided), see Deployment: HomeStack;
  external mounts for media are a pending HomeStack feature.
* Testing: **e2e only, no unit tests** (decided); today's suite is deleted in
  step 1.
* HTTP framework: **FastAPI + uvicorn** (decided); HTTP client for the client commands:
  **httpx**. New runtime dependencies, to be documented in `README.md` and
  `docs/getting-started/` when steps 5–6 land.

## Open decisions

* Web client: vanilla JS (zero build) vs. keeping Jinja/HTMX fragments rendered
  by `vbc-web` from API data. Recommended: vanilla JS, because HTMX polling
  cannot extrapolate counters.
* What `vbc run <dirs>` becomes when legacy is deleted. Recommended: a thin
  launcher that starts `serve` on loopback (free port) and
  attaches `tui` in the same terminal (single code path; no in-process
  dashboard is carried over).
* GPU metrics: v1 needs the latest sample **and** a bounded history, because
  the TUI sparkline shows history today and `vbc tui` is in phase 1:
  `GET /api/v1/gpu?since=<seq>` returns new samples (ring buffer, `410` when
  `since` is too old, like `/errors`). Anchors/extrapolation for GPU are
  deferred.
