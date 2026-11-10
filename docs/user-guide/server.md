# Server and Clients

VBC can run as a headless server (`vbc serve`) that you control from the
terminal UI (`vbc tui`) or with one-shot commands (`vbc status`, `vbc threads`,
...). The server does all the work and owns all files. Clients only send
commands and read status. Videos never travel over the connection.

To compress folders on this machine from one terminal, `vbc run [DIRS]`
starts a private server and this dashboard together and stops both when you
quit (see [CLI Reference](cli.md#vbc-run)). The design is described in
[Service Split Plan](../architecture/service-split.md).

## Start the server

```bash
uv run vbc serve --config conf/vbc.yaml          # http://127.0.0.1:8770
uv run vbc serve --config conf/vbc.yaml --demo   # simulated processing, no file IO
```

* Input folders come from `input_dirs` in the config. Without any, the server
  starts idle and waits for folders added through `vbc dirs add`.
* After every pass the server stays up (status `idle`) and waits for a refresh
  (`vbc refresh`, key `R`) or a watcher event.
* `vbc stop` stops processing only (running jobs finish; `--interrupt` stops
  them now). The server stays up in status `stopped`, and `vbc refresh`
  resumes. Nothing in the API or the clients ends the server process.
* Logs go to stdout. Add `--log-file PATH` to also write them to a file.
* The process ends only on a signal. The first SIGTERM/SIGINT lets running
  jobs finish (at most `server.stop_timeout_s`, default 600 s), and a second
  SIGINT interrupts them.

### Settings

```yaml
server:
  host: 127.0.0.1        # 0.0.0.0 or an interface IP for access from other hosts
  port: 8770
  allowed_hosts: []      # extra names; own host name, <host>.<search domain> and IPs pass anyway
  stop_timeout_s: 600
storage:
  roots: []              # extra roots allowed off loopback (in addition to detected media mounts)
```

Environment overrides (for containers): `VBC_SERVER_HOST`, `VBC_SERVER_PORT`,
`VBC_ALLOWED_HOSTS`, `VBC_STORAGE_ROOTS` (comma-separated; they replace the lists from the file), `VBC_GPU` (`auto`
probes NVENC at every start and falls back to CPU SVT-AV1 when unavailable;
`cpu` / `false` forces CPU).

### Security model

The API is plain HTTP with no login. It controls the server: start or stop
work, change threads, and add, remove or toggle folders inside the allowed
storage roots. It cannot read, upload, download or delete media, and it cannot
change source deletion policies for existing files without verification. Bind
to a non-loopback address only on a trusted network. When bound off loopback,
the allowed storage roots are `storage.roots` plus every media mount of the
process (detected from `/proc/self/mountinfo` on each use), and `PUT /dirs`
rejects paths outside those roots. For access from outside, publish it behind a
reverse proxy with its own authentication (for example Pangolin) or use an SSH
tunnel.

## Terminal UI

```bash
uv run vbc tui                       # server at $VBC_API or http://127.0.0.1:8770
uv run vbc tui --api http://host:8770
```

The dashboard is fed by the API (`vbc run` shows the same one). Keys that
talk to the server:

| Key | `vbc tui` |
|---|---|
| `<` / `>` | change server threads (absolute target sent to the server) |
| `R` | rescan; also resumes after a pause on error or a stop |
| `S` | graceful stop of processing (active jobs finish, no new jobs start; press `S` again while stopping to cancel; `R` resumes; the server keeps running) |
| `Q`, `Ctrl+Q`, `Ctrl+C` | quit the client at once; the server keeps running (in the Dirs path input, `q` is a letter) |
| `Shift+U` | unlock keyboard after auto-lock (auto-locks after 60 s without keys, configurable via `VBC_TUI_LOCK_AFTER_S`; `0` disables) |
| `D` (Dirs tab) | edit the server's folder list; `R` scans server media mounts and offers new folders as disabled entries; `S` saves the whole list, and errors from the server are shown inline |

Paths in the Dirs and IO tabs are paths on the server. Pasted text (bracketed
paste) never triggers hotkeys; in the Dirs add-path input it types into the
path buffer. When the server encodes on GPU, the metrics panel displays GPU
sparklines (`nvidia-smi`, or `nvtop` fallback); when encoding on CPU, it shows
CPU utilization graphs. When the server is exiting on a signal, the header
shows the countdown to `server.stop_timeout_s`.

When the connection drops, the header shows `DISCONNECTED` and the footer shows
the reason. Progress keeps extrapolating, and the client reconnects on its own.
Connection events are logged to `~/.local/state/vbc/tui.log` (override with
`--log-file`).

The server logs each TUI session: `Client connected: tui <id> from <host>`
on its first request, `Client disconnected (quit)` when it exits, and
`Client disconnected (no requests for 20 s)` when it vanished (killed,
network gone). One-shot commands are not logged as sessions.

## One-shot commands

All commands take `--api URL` (default `$VBC_API` or `http://127.0.0.1:8770`)
and `--json`.

| Command | What it does |
|---|---|
| `vbc status [--watch]` | run status, counters, active jobs, queue, recent jobs |
| `vbc threads N` | set concurrent jobs, prints the clamped value |
| `vbc refresh` | rescan the input folders |
| `vbc stop [--interrupt \| --cancel]` | stop processing (jobs finish / now) or withdraw a pending stop; the server keeps running |
| `vbc dirs` | list the input folders and whether they exist on the server |
| `vbc dirs add\|remove\|enable\|disable PATH`, `vbc dirs set PATH...` | edit the list (put `--api` and `--json` before the subcommand) |
| `vbc dirs scan` | scan server media mounts and offer new folders with videos or manifests as disabled entries |
| `vbc errors [--since ID]` | this session's failed files |
| `vbc logs [--follow] [--level LEVEL] [--since SEQ]` | server application log |
| `vbc config` | effective configuration |
| `vbc config get [KEY...]` | settings as stored in the server's config file (defaults filled in); a section name lists its settings, no key lists all |
| `vbc config set KEY=VALUE... [--no-restart]` | change single settings; VALUE is YAML (`40`, `true`, `'[mp4, mov]'`, `"{'GOPR.*': 180}"`); the server restarts to apply them (running jobs finish first) |
| `vbc config unset KEY... [--no-restart]` | remove settings from the file so their defaults apply |
| `vbc config --push FILE [--no-restart]` | replace the whole config file (the server keeps its folders, bind address, storage) |
| `vbc sources cleanup [--delete] [--dir DIR]` | find sources on the server that already have a verified output in `_out`; `--delete` removes them |
| `vbc manifests restore PATH [--dry-run]` | restore a failed manifest `.json` and its archived sources back to the metadata folder on the server |
| `vbc manifests repair PATH [--dry-run]` | recompress supported failed manifests (FFmpeg exit 244/-12: video without audio) on the server |
| `vbc versions` | versions the server runs with: VBC, ffmpeg and its libraries, exiftool, nvtop, NVIDIA driver, image packages (`GET /api/v1/versions`) |
| `vbc log-level LEVEL` | change the server log level at runtime |

Changing single settings (`PATCH /api/v1/config`) is safe with several clients:
each change carries the value the client read (`GET /api/v1/config/values`),
and the server refuses all of them (409, with its current values) when any of
those settings was changed by someone else in the meantime. A list or a
mapping such as `autorotate.patterns` is one value and is set whole. The
server-owned sections (`input_dirs`, `server`, `storage`, `general.log_path`, ...)
are not editable this way; the Dirs list has its own commands.

Exit status: `0` ok, `1` rejected by the server (the reason is printed),
`2` usage error, `3` server unreachable.

## HTTP API

The OpenAPI description is in `docs/api/openapi.json`. It is regenerated with
`uv run python scripts/export-openapi.py`. Health endpoints for orchestration
are `/health/live` and `/health/ready` (`application/health+json`), plus
`/.well-known/homestack/status`.
