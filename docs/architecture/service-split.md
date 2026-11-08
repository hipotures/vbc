# Service Split Plan (server / API / clients)

Status: **proposal**. Scope: VBC only. Deployment target (container, VM, LXC)
and GPU device handling are explicitly out of scope; everything below assumes
the server and the clients run as ordinary local processes that may later live
on different hosts.

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
  (add/remove/reorder/enable input folders, threads, refresh, shutdown, log
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
rewritten `vbc.` → `vbc_legacy.`), its tests to `tests/legacy/`. The new
`vbc/` is built from scratch next to it, **taking over from legacy only what
is needed**, by copying whole files (`cp`) or cutting out fragments. When the
new clients reach parity, `vbc_legacy/` and `tests/legacy/` are deleted in one
commit, so no old code survives that was not deliberately carried over.

```
vbc_legacy/      # frozen copy of today's vbc/ — `vbc run <dirs>` runs from here during migration
tests/legacy/    # today's tests, imports switched to vbc_legacy
vbc/             # new: domain, config, infrastructure, pipeline, service, contracts, clients
tests/unit/, tests/integration/   # tests for the new tree
```

Rules:

* **Legacy is frozen.** No features; a critical bug fix is made in both trees.
* **Two separate packages.** Legacy and new code never import each other;
  the only bridge is the characterisation-test scenario run against both.
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
* **Tests travel with code.** Copying a module copies (and adapts) its tests
  from `tests/legacy/`.
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
| `vbc/contracts/` | Transport-only Pydantic DTOs with `schema_version`. The only module clients may share. `docs/api/openapi.json` generated from it and verified by a test. |

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
last snapshot, `R` → `POST /refresh`, `S` → `POST`/`DELETE /shutdown`
depending on `shutdown_requested`, Ctrl+C → detach the client (does **not**
stop the server; interrupting the server is an explicit command). Overlay
tabs fetch `/config`, `/io`, `/dirs`, `/errors`, `/logs` on demand.

One-shot commands (phase 1): non-interactive commands for scripts and quick
checks, **peers of `serve` and `tui`** in one flat `vbc` command set (no
separate `ctl` group), one per endpoint: `vbc status` (one-shot or
`--watch`), `vbc threads N`, `vbc refresh`, `vbc shutdown
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
   (`/srv/producer` → `/srv/producer`, `/data/videos` + `T_out` + `T_err`, or their common
   parent). Manifests, suffix outputs and existing configs keep working
   unchanged. Alternatively use explicit `output_dirs`/`errors_dirs` inside the
   mounted tree instead of suffix mode.
2. **Declared storage roots (server config + API).** Optional on loopback,
   mandatory when the API is bound to any other address (see Security). New
   config section, e.g.:

   ```yaml
   storage:
     roots:
       - {name: tk, path: /srv/producer}
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
   `storage.path_map: {"/srv/producer": "/data/ai"}` applied to manifest `inputs` and
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
* The log file stays as is. Switching `mode="w"` to append/rotation is
  worth doing for a long-running server, but it is an independent change.
* Live tail uses the same long-poll mechanism (`wait=` parameter); no separate
  streaming protocol.

## API (v1)

All responses carry `schema_version`, `rev` and `server_time` (epoch ms, used by
clients to correct clock skew).

Read:

| Endpoint | Content |
|---|---|
| `GET /api/v1/state?since=<rev>&wait=<s>` | Main snapshot (run status, counters, bytes, throughput anchors, active jobs with progress anchors, last N recent jobs, queue count, `shutdown_requested`, `config_rev`, `errors_last_id`). Long-poll: returns immediately if `rev > since`, otherwise holds up to `wait` (≤30 s) and returns `204 No Content` on timeout (the client simply polls again). Plain polling uses `ETag: "<rev>"` and returns `304` only for a request carrying `If-None-Match`. |
| `GET /api/v1/gpu?since=<seq>` | Latest GPU sample (`gpu_data` today) plus samples newer than `seq` from a bounded history (for the TUI sparkline); `enabled: false` when monitoring is off. Anchors are deferred (see Open decisions). |
| `GET /api/v1/queue?offset&limit` | Pending files (can be 10k+, never inside the snapshot). |
| `GET /api/v1/jobs/recent?limit` | Longer activity history (bounded ring, e.g. 500). |
| `GET /api/v1/errors?since&limit` | Session error log entries (see Logs). |
| `GET /api/v1/logs?since&limit&level&wait` | Application log ring buffer (see Logs). |
| `GET /api/v1/config` | Effective config (today's Settings tab lines, structured). |
| `GET /api/v1/io` | Input dir stats, output/error dirs, queue sort/seed, log path. |
| `GET /api/v1/dirs` | Ordered `[{path, enabled}]` + `dirs_rev`. |
| `GET /health/live`, `GET /health/ready` | Liveness / readiness. |

Commands (absolute and idempotent where possible):

| Endpoint | Maps to |
|---|---|
| `PUT /api/v1/threads {"target": n}` | New `SetThreadCount` event (replaces client-side ±1). The orchestrator clamps and the response returns the **effective** value; `threads_min`/`threads_max` are exposed in `/config` (today `1..8` hard-coded, to be moved to config). |
| `POST /api/v1/refresh` | `RefreshRequested`. This is also the **only** way to resume after `paused_on_error` or `waiting_for_input`: it always triggers a full rediscovery; there is no "continue without rescan". |
| `POST /api/v1/shutdown {"mode": "graceful"\|"interrupt"}` | New absolute `SetShutdownRequested(true)` / `InterruptRequested`. Repeating it is a no-op. |
| `DELETE /api/v1/shutdown` | `SetShutdownRequested(false)`: cancels a pending graceful shutdown (what today's second "S" does). `RequestShutdown` stays as the TUI toggle, translated to the absolute event. |
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
* bump `rev` only if `|predicted - actual| > 2 p.p.`, or the rate changed by more
  than ~20 %, or 30 s passed since the last anchor (cap on staleness);
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

## Process model

| Command | What runs |
|---|---|
| `vbc serve [--config] [--host] [--port]` | `bootstrap` + API, headless, no Rich, no keyboard. Input dirs from config (positional args optional). `--demo` uses `DemoOrchestrator`. |
| `vbc tui [--api http://host:port]` | TUI client only (phase 1). |
| `vbc status`, `vbc threads N`, `vbc refresh`, `vbc shutdown`, `vbc dirs`, `vbc errors`, `vbc logs`, `vbc config` | One-shot API commands (phase 1). `--api` on every client command, also from env (`VBC_API`) or client config; default `http://127.0.0.1:<default port>`. |
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
  starts one automatically. The process ends only on `POST /shutdown` or a
  signal.
* **Threads and signals.** uvicorn owns the main thread and its event loop;
  `bootstrap` returns an object with `start()` / `stop()` that runs the
  orchestrator in its own thread. uvicorn installs its own SIGINT/SIGTERM
  handlers, so the server hooks its shutdown: SIGTERM → `SetShutdownRequested`
  (graceful), a second SIGINT → `InterruptRequested`; uvicorn stops after the
  orchestrator thread has joined. `POST /shutdown` goes through the same path.

## Migration steps

Every step leaves both test suites green (`tests/legacy/` and the new tests)
and `vbc run <dirs>` (legacy) working.

1. **Move to legacy.** `git mv vbc vbc_legacy`, `git mv` today's tests to
   `tests/legacy/`, rewrite imports `vbc.` → `vbc_legacy.`; new
   `vbc/cli.py` with only `vbc run <dirs>` delegating to
   `vbc_legacy.main`. Pure mechanical commit; the full
   legacy suite must pass unchanged.
2. **Characterisation tests (on legacy).** A scripted event sequence and a
   `DemoOrchestrator` run, asserting the resulting legacy `UIState` business
   fields. Written as a scenario + expected values that can be run against
   any read model, so the same scenario later checks the new `StateStore`.
   Must cover all `UIManager` subscriptions (35 today), not only counters.
3. **Copy the core into `vbc/`, fixing leaks in the copy.** `domain/`,
   `config/`, `infrastructure/` adapters and watchers, `event_bus`,
   `logging`, `pipeline/` (with their tests), then:
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
   dead-reckoning thresholds, ETA anchor, GPU sample history. The step-2
   scenario passes against `StateStore`; threshold logic unit-tested with an
   injected clock.
5. **API + `vbc serve`.** `service/bootstrap.py` (wiring cut out of legacy
   `compress()`); FastAPI app with the endpoints above, long-poll via
   `asyncio.Event` set with `loop.call_soon_threadsafe` from the bus
   callback; Host/Origin allowlist, health, OpenAPI artifact + drift test; lifecycle and
   signals as in Process model. Integration test: `serve --demo` + HTTP
   client.
6. **One-shot commands.** `clients/api_client.py` + `vbc status`/`threads`/
   `refresh`/`shutdown`/`dirs`/`errors`/`logs`/`config`. Comes before the
   TUI because it is small and exercises every endpoint end to end
   (integration tests against `serve --demo`).
7. **`vbc tui`.** Panels, overlays and `format_*` helpers copied from
   `vbc_legacy/ui/` into `clients/tui/`, data source switched from `UIState`
   to the client-side snapshot (DTOs + client view state); long-poll thread,
   local extrapolation ticker, key → command mapping. Parity check: the same
   demo session in legacy `vbc run --demo` and in `serve --demo` + `vbc tui`
   shows the same panels. Phase 1 ends here (docs for `serve`, `tui`, `run` and the
   one-shot commands
   land with it).
8. **Web client (later).** `frontends/web/` (own entry point `vbc-web`):
   static page + small JS client (long-poll loop + local ticker for
   extrapolation) + proxy, reaching parity with the legacy HTMX dashboard.
9. **Delete legacy.** Once `vbc run <dirs>` has its replacement (see Open
   decisions) and the web client exists: remove `vbc_legacy/` and
   `tests/legacy/` in one commit. If the web client is still far off, legacy
   may be deleted after step 7 and `--web` dropped until step 8 lands.
10. **Docs.** `docs/architecture/` (this page → final design), API reference,
    user guide for `serve`/`tui`/`run`/one-shot commands (phase 1) and `vbc-web` (later);
    README dependency notes (FastAPI, uvicorn, httpx).

## Decisions

* Language: **Python stays** (decided) for the server and for the clients
  (`vbc tui`, the one-shot commands, `vbc-web`). No Rust rewrite; mypowers is referenced
  only for its API conventions, not its implementation stack.
* Security (decided): plain HTTP, no token, no TLS; trusted network only,
  Host/Origin allowlist, `storage.roots` mandatory off loopback.
* Command line (decided): one flat set of peer subcommands — `vbc serve`,
  `vbc tui`, `vbc run <dirs>`, `vbc status`, `vbc threads`, `vbc refresh`,
  `vbc shutdown`, `vbc dirs`, `vbc errors`, `vbc logs`, `vbc config`. No
  `ctl` group.
* Delivery order: **server + one-shot commands + `vbc tui` first** (phase 1, steps
  1–7), web client later (step 8), legacy deleted in step 9.
* Code strategy: **legacy package + new tree** (decided): today's code moves
  to `vbc_legacy/`, the new `vbc/` takes over only what it needs, legacy is
  deleted at the end.
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
