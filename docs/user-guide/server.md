# Server and Clients

VBC can run as a headless server (`vbc serve`) that you control from the
terminal UI (`vbc tui`) or with one-shot commands (`vbc status`, `vbc threads`,
...). The server does all the work and owns all files. Clients only send
commands and read status. Videos never travel over the connection.

During the migration the classic in-process dashboard is still available as
`vbc run <dirs>` (see [CLI Reference](cli.md)). The design is described in
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
  allowed_hosts: []      # extra host names accepted in Host/Origin headers
  stop_timeout_s: 600
storage:
  roots: []              # required when host is not loopback
```

Environment overrides (for containers): `VBC_SERVER_HOST`, `VBC_SERVER_PORT`,
`VBC_ALLOWED_HOSTS`, `VBC_STORAGE_ROOTS` (comma-separated).

### Security model

The API is plain HTTP with no login. It controls the server: start or stop
work, change threads, and add, remove or toggle folders inside `storage.roots`.
It cannot read, upload, download or delete media, and it cannot change
encoding or source policies. Bind to a non-loopback address only on a trusted
network. The server refuses to start that way without `storage.roots`. For
access from outside, publish it behind a reverse proxy with its own
authentication (for example Pangolin) or use an SSH tunnel.

## Terminal UI

```bash
uv run vbc tui                       # server at $VBC_API or http://127.0.0.1:8770
uv run vbc tui --api http://host:8770
```

It is the same dashboard as `vbc run`, fed by the API. Keys are the same, with
these differences:

| Key | `vbc tui` |
|---|---|
| `<` / `>` | change server threads (absolute target sent to the server) |
| `R` | rescan; also resumes after a pause on error or a `vbc stop` |
| `Ctrl+Q` | quit after the jobs that are running now have finished (the server keeps working) |
| `Ctrl+C` | quit now; the server keeps running |
| `S` | no action (stopping processing is `vbc stop`; the TUI never stops the server) |
| `D` (Dirs tab) | edit the server's folder list; `S` saves the whole list, and errors from the server are shown inline |

Paths in the Dirs and IO tabs are paths on the server. When the connection
drops, the header shows `DISCONNECTED` and the footer shows the reason. Progress
keeps extrapolating, and the client reconnects on its own. Connection events
are logged to `~/.local/state/vbc/tui.log` (override with `--log-file`).

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
| `vbc errors [--since ID]` | this session's failed files |
| `vbc logs [--follow] [--level LEVEL] [--since SEQ]` | server application log |
| `vbc config` | effective configuration |
| `vbc log-level LEVEL` | change the server log level at runtime |

Exit status: `0` ok, `1` rejected by the server (the reason is printed),
`2` usage error, `3` server unreachable.

## HTTP API

The OpenAPI description is in `docs/api/openapi.json`. It is regenerated with
`uv run python scripts/export-openapi.py`. Health endpoints for orchestration
are `/health/live` and `/health/ready` (`application/health+json`), plus
`/.well-known/homestack/status`.
