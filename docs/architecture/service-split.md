# Service Split Plan (server / API / clients)

Status: **proposal**. Scope: VBC only. Deployment target (container, VM, LXC), GPU
handling and the Rich TUI rewrite are explicitly out of scope; everything below
assumes the server and the clients run as ordinary local processes that may
later live on different hosts.

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
                                   │ HTTP (long-poll), bearer token
            ┌──────────────────────┼──────────────────────┐
            ▼                      ▼                      ▼
     vbc-web (separate       future TUI client       curl / scripts
     process, own port)
```

* The **server** owns all business state and all side effects (encoding, config
  writes, marker files, repair).
* **Clients** own only presentation state (tabs, cursors, palettes, pagination,
  formatting) and never import server internals.
* The in-process `EventBus` stays the backbone inside the server. It is **not**
  exported over the network; the network sees a versioned read model and a small
  command API.

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
| Infrastructure depends on UI | `infrastructure/web_server.py:29`, `infrastructure/gpu_monitor.py:10` import `UIState` | Inverted dependency |
| Presentation in pipeline | `pipeline/repair.py`, `pipeline/error_file_mover.py` use `rich.console` / `rich.progress` | Headless server must not print Rich output |
| TUI-only events in the domain | `domain/events.py` `Dirs*` (overlay/sparkline events already live in `ui/keyboard.py`) | Keystroke-level events are not domain events |
| Composition root is one function | `main.py:424-830` always builds Dashboard, Keyboard, UIManager, web server | No headless entry point |
| Deltas, not values, for threads | `ThreadControlEvent(change=±1)` | Two clients pressing `>` race; network commands must be absolute and idempotent |

## Responsibility split

### Server (`vbc serve`)

Stays as is: `domain/`, `config/`, `infrastructure/` (ffmpeg, ffprobe,
exif_tool, manifest_watcher, logging, event_bus), `pipeline/`.

New package `vbc/service/`:

| Module | Responsibility |
|---|---|
| `service/state_store.py` | Subscribes to domain events, maintains the read model (the business half of today's `UIState` + `UIManager` handlers), bumps `rev`, computes rates/ETA anchors (see below). Bus callbacks only mutate memory under a lock and notify; no I/O. |
| `service/commands.py` | `CommandService`: validates client commands, persists config (`save_dirs_config` moves here), publishes domain events. |
| `service/api.py` | HTTP app (FastAPI + uvicorn — **decided**; same stack as mypowers, OpenAPI for free). Thin: translate HTTP ↔ StateStore/CommandService. |
| `service/bootstrap.py` | Headless composition root extracted from `main.py`: config, bus, adapters, orchestrator, StateStore, CommandService. Reused by local mode and `serve`. |
| `vbc/contracts/` | Transport-only Pydantic DTOs with `schema_version`. The only module clients may share. `docs/api/openapi.json` generated from it and verified by a test. |

### Clients

Own only view state: overlay visibility/active tab, dim level, sparkline
preset/palette/metric, logs page index, Dirs editor cursor/input buffer/staged
edits, unicode stripping, every `format_*` helper. Dirs editing becomes "edit
locally, then send the whole ordered list" (`PUT /dirs`).

`vbc-web`: separate process, separate entry point, own port. Serves static
files and proxies `/api/*` to the server, injecting the bearer token
(backend-for-frontend: no CORS, token never reaches the browser). Replaces
`infrastructure/web_server.py` and the `--web` flag.

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
2. **Declared storage roots (server config + API).** New config section, e.g.:

   ```yaml
   storage:
     roots:
       - {name: tk, path: /srv/producer}
       - {name: videos, path: /data/videos}
   ```

   * `PUT /api/v1/dirs` rejects (`422`) any path, and its derived output/error
     dir, that is not inside a declared root. Without this, any API client
     could make the server read and write anywhere it has permissions.
   * `GET /api/v1/roots` and `GET /api/v1/fs?root=&path=` (directories only,
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

## API (v1)

All responses carry `schema_version`, `rev` and `server_time` (epoch ms, used by
clients to correct clock skew).

Read:

| Endpoint | Content |
|---|---|
| `GET /api/v1/state?since=<rev>&wait=<s>` | Main snapshot (run status, counters, bytes, throughput anchors, active jobs with progress anchors, last N recent jobs, queue count). Long-poll: returns immediately if `rev > since`, otherwise holds up to `wait` (≤30 s) and returns `304`. `ETag: rev` for plain polling. |
| `GET /api/v1/queue?offset&limit` | Pending files (can be 10k+, never inside the snapshot). |
| `GET /api/v1/jobs/recent?limit` | Longer activity history (bounded ring, e.g. 500). |
| `GET /api/v1/errors?offset&limit` | Session error log entries. |
| `GET /api/v1/config` | Effective config (today's Settings tab lines, structured). |
| `GET /api/v1/io` | Input dir stats, output/error dirs, queue sort/seed, log path. |
| `GET /api/v1/dirs` | Ordered `[{path, enabled}]` + `dirs_rev`. |
| `GET /health/live`, `GET /health/ready` | Liveness / readiness. |

Commands (absolute and idempotent where possible):

| Endpoint | Maps to |
|---|---|
| `PUT /api/v1/threads {"target": n}` | New `SetThreadCount` event (orchestrator clamps; replaces client-side ±1) |
| `POST /api/v1/refresh` | `RefreshRequested` (also "R" in waiting/paused state) |
| `POST /api/v1/shutdown {"mode": "graceful"\|"interrupt"}` | `RequestShutdown` / `InterruptRequested` |
| `PUT /api/v1/dirs` + `If-Match: <dirs_rev>` | Validate → `save_dirs_config` → `InputDirsChanged` + `RefreshRequested`; `412` on stale rev |

Security: bind `127.0.0.1` by default; bearer token from a 0600 file,
constant-time compare; Host/Origin allowlist (same model as mypowers).

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
| `vbc serve [--config]` | `bootstrap` + API, headless, no Rich, no keyboard. Input dirs from config (positional args optional). `--demo` uses `DemoOrchestrator`. |
| `vbc-web --api http://host:port --token-file …` | Web client only. |
| `vbc <dirs>` (today) | Kept during migration: same `bootstrap` + current TUI subscribed in-process. Converting the TUI into an API client is out of scope. |

## Migration steps

Every step leaves the test suite green and `vbc <dirs>` working.

1. **Characterisation tests.** Drive a scripted event sequence (or
   `DemoOrchestrator`) and assert the resulting `UIState` business fields. This
   is the parity oracle for steps 2–3.
2. **Remove layer leaks.**
   * `pipeline/repair.py`, `pipeline/error_file_mover.py`: replace Rich output
     with events/logging (CLI utilities can render them).
   * Move `save_dirs_config` + `InputDirsChanged` publishing from
     `UIManager.on_dirs_apply_changes` into `service/commands.py`; the TUI calls
     the command service.
   * Move the `Dirs*` events from `domain/events.py` to `ui/` (next to the
     overlay events in `ui/keyboard.py`).
   * Add `SetThreadCount(target)`; keep `ThreadControlEvent` as a TUI-side
     convenience translated to an absolute target.
3. **Split `UIState`.** Introduce `service/state_store.py` with the business
   fields and the handlers moved from `UIManager`. Keep `ui/view_state.py` for
   the view-only fields. Dashboard reads both. `infrastructure/web_server.py`
   reads `StateStore` (temporary, deleted in step 6).
4. **Contracts + anchors.** `vbc/contracts/` DTOs, `rev`, rate EWMA,
   dead-reckoning thresholds, ETA anchor. Unit tests for the threshold logic
   (deterministic clock injected).
5. **API + `vbc serve`.** Extract `service/bootstrap.py` from `main.py`; FastAPI
   app with the endpoints above, long-poll via `asyncio.Event` set with
   `loop.call_soon_threadsafe` from the bus callback; auth, health, OpenAPI
   artifact + drift test. Integration test: `serve --demo` + HTTP client.
6. **Separate web client.** `frontends/web/` (own entry point `vbc-web`): static
   page + small JS client (long-poll loop + local ticker for extrapolation) +
   proxy. Reach parity with the HTMX dashboard, then delete
   `infrastructure/web_server.py` and `--web`.
7. **Docs.** `docs/architecture/` (this page → final design), API reference,
   user guide for `serve`/`vbc-web`; README dependency notes (FastAPI, uvicorn).

## Decisions

* HTTP framework: **FastAPI + uvicorn** (decided). New runtime dependencies,
  to be documented in `README.md` and `docs/getting-started/` when step 5 lands.

## Open decisions

* Web client: vanilla JS (zero build) vs. keeping Jinja/HTMX fragments rendered
  by `vbc-web` from API data. Recommended: vanilla JS, because HTMX polling
  cannot extrapolate counters.
* Whether `vbc <dirs>` should eventually always start a local server and attach
  the TUI over HTTP (single code path) or keep the in-process mode.
* GPU metrics: same pattern (server-side producer → read model → anchors);
  deferred.
