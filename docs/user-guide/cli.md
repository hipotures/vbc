# CLI Reference

Complete reference for VBC command-line interface.

## Subcommands Overview

`vbc` provides peer subcommands for the server and its clients (a bare `vbc <dirs>` is rejected with a hint to use `vbc run <dirs>`):

| Subcommand | Description |
|------------|-------------|
| `vbc serve [DIRS...] [--config PATH] [--host HOST] [--port PORT] [--demo] [--demo-config PATH] [--log-file PATH]` | Run the headless server (`HTTP API` on `server.host:server.port`, logs on stdout); `DIRS` replace the config's folders for this process only |
| `vbc tui [--api URL] [--log-file PATH]` | Live Rich dashboard connected to a running server |
| `vbc status`, `vbc threads`, `vbc refresh`, `vbc stop`, `vbc dirs`, `vbc errors`, `vbc logs`, `vbc config`, `vbc versions`, `vbc log-level`, `vbc sources cleanup`, `vbc manifests restore\|repair` | One-shot API client commands (`--api URL`, `--json`) |
| `vbc run [DIRS...] [--config PATH] [--log-file PATH]` | Private server on loopback plus the dashboard in one terminal (documented below) |

For full details on `vbc serve`, `vbc tui`, and all one-shot client commands, see [Server and Clients](server.md).

## `vbc run`

```bash
uv run vbc run [DIRS...] [--config PATH] [--log-file PATH]
```

`vbc run` starts a private `vbc serve` on loopback (a free port), opens the
dashboard (`vbc tui`) in the same terminal and stops that server when you quit
the dashboard. It is the one-terminal way to compress folders on this machine;
for a server that keeps running (HomeStack, another host) use `vbc serve` and
attach `vbc tui` to it.

| Argument / option | Meaning |
|---|---|
| `DIRS` | Folders to compress. They **replace** the config's `input_dirs` for this run only: nothing is saved, and the Dirs list is read-only (the server answers `409` to edits and mount scans). Without `DIRS` the config's list applies and the Dirs tab edits it as usual. Output and error folders follow `suffix_output_dirs` / `suffix_errors_dirs` (`<dir>_out`, `<dir>_err`). |
| `--config PATH`, `-c PATH` | Config file of the private server (default `conf/vbc.yaml`). |
| `--log-file PATH` | Dashboard log (default `~/.local/state/vbc/tui.log`). The server log goes to `~/.local/state/vbc/run-server.log`. |

There are **no per-run settings on the command line** (no `--threads`,
`--gpu`, `--quality`, …). Settings come from:

* the config file: for a special run, copy the global config, change it and
  pass it with `--config` (see [Configuration](../getting-started/configuration.md#environment-specific-configs));
* `VBC.YAML` in a folder, for everything below it (see
  [Local Overrides](../getting-started/configuration.md#local-overrides-vbcyaml));
* the dashboard at run time: `<` / `>` change threads, `S` stops gracefully,
  `R` rescans.

After the queue is done the server waits and the dashboard shows the result;
`Q`, Ctrl+Q or Ctrl+C quit and stop the server. Quitting while jobs are running
interrupts them (partial outputs are removed, sources stay untouched).

```bash
# Compress one folder with the settings from conf/vbc.yaml
uv run vbc run /videos/2026-trip

# Same, with a prepared config for archive quality
uv run vbc run /videos/2026-trip --config conf/archive.yaml

# The folders from the config file
uv run vbc run
```

### Where the former `vbc run` options went

| Former option | Setting (config file; ★ also in `VBC.YAML`) |
|---|---|
| `--threads N` | `general.threads` (live: `<` / `>`) |
| `--quality N` | `-cq` / `-crf` in `gpu_encoder` / `cpu_encoder` args; ★ `cq` |
| `--quality-mode`, `--bps`, `--minrate`, `--maxrate` | ★ `general.quality_mode`, `bps`, `minrate`, `maxrate` |
| `--gpu` / `--cpu` | ★ `general.gpu` |
| `--queue-sort`, `--queue-seed` | `general.queue_sort`, `general.queue_seed` |
| `--skip-av1`, `--min-size`, `--min-ratio`, `--camera` | ★ `general.skip_av1`, `min_size_bytes`, `min_compression_ratio`, `filter_cameras` |
| `--rotate-180` | ★ `general.manual_rotation` |
| `--clean-errors`, `--verify-fail-action` | ★ `general.clean_errors`, `verify_fail_action` |
| `--debug` | ★ `general.debug` (live: `vbc log-level`) |
| `--log-path` | `general.log_path` |
| `--wait` / `--no-wait`, `--bell` | gone: the server always waits after a pass; quit the dashboard to finish |
| `--web`, `--web-port` | gone until the web client (step 9 of the service-split plan) |
| `--demo`, `--demo-config` | `vbc serve --demo [--demo-config PATH]` + `vbc tui` |

### Audio

Audio handling is automatic:
- Lossless audio (`pcm_*`, `flac`, `alac`, `truehd`, `mlp`, `wavpack`, `ape`, `tta`) is transcoded to AAC at 256 kbps.
- AAC/MP3 are stream-copied.
- Other/unknown codecs are transcoded to AAC at 192 kbps for MP4 compatibility.
- Files without audio remain silent.

**Debug hint:** with `general.debug: true` the log shows `AUDIO_MODE` with the detected codec and action.

## Configuration Priority

1. The config file (`--config`, default `conf/vbc.yaml`).
2. `VBC.YAML` in the input tree (nearest ancestor of each file wins), for the
   keys it allows.
3. For `vbc serve` only: deployment environment variables (below) override
   the bind address, allowed hosts, storage roots and GPU use.

## Environment Variables

- **Client commands & `vbc tui`**:
  - `VBC_API`: Default server URL when `--api` is omitted (fallback: `http://127.0.0.1:8770`).
  - `VBC_TUI_LOCK_AFTER_S`: Seconds of keyboard inactivity before `vbc tui` auto-locks (default `60`, `0` disables).
- **Server (`vbc serve`)**:
  - `VBC_SERVER_HOST`, `VBC_SERVER_PORT`: Override `server.host` and `server.port`.
  - `VBC_ALLOWED_HOSTS`: Comma-separated `Host`/`Origin` names; replaces `server.allowed_hosts`.
  - `VBC_STORAGE_ROOTS`: Comma-separated storage roots; replaces `storage.roots` (detected media mounts are still added off loopback).
  - `VBC_GPU`: `auto` (default in container: probes `av1_nvenc` at start and falls back to CPU SVT-AV1 if unavailable), `true`/`1`, or `cpu`/`false`/`0`.
  - `HOMESTACK_HEARTBEAT_SECONDS`: Interval of the server's self-heartbeat (a real `GET /api/v1/state`), whose result `/health/ready` and the HomeStack status report (default `60`).

## Exit Codes

### `vbc run` and `vbc serve`

| Code | Meaning |
|------|---------|
| 0 | Normal end (`vbc run`: the dashboard was quit; `vbc serve`: ended by `SIGTERM`/`SIGINT`) |
| 2 | Invalid config, a folder that does not exist, or (`vbc run`) the private server did not start; see `~/.local/state/vbc/run-server.log` |

### Client Commands (`vbc status`, `threads`, `dirs`, `config`, ...)

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Rejected by the server (e.g. validation error, `409`/`412`/`422`) |
| 2 | Usage error (invalid CLI arguments) |
| 3 | Server unreachable |

## Output Files

### Compressed Videos

```
{INPUT_DIR}_out/{relative_path}{ext}
```

By default, output files are `.mp4`. If encoder args include a format flag (e.g. `-f matroska` or `-f mov`), VBC uses the matching file extension (e.g. `.mkv` or `.mov`).
Default output directory uses `suffix_output_dirs` (default `_out`); you can override per-input with `output_dirs`.

**Example:**
```
Input:  /videos/subfolder/video.avi
Output: /videos_out/subfolder/video.mp4
```

### Log File

```
/tmp/vbc/compression.log
```

Detailed log of all operations (INFO and ERROR levels). Override with `general.log_path`.

### Error Markers

```
{INPUT_DIR}_out/{relative_path}.err   # during run
{INPUT_DIR}_err/{relative_path}.err   # final location after run (default suffix_errors_dirs)
```

Created for failed compressions. Contains error message.
These markers are written under the output directory during processing and moved to
`errors_dirs`/`suffix_errors_dirs` after the run completes (if configured).

**Example:**
```
$ cat /videos_err/video.err
Hardware is lacking required capabilities
```

## Next Steps

- [Runtime Controls](runtime-controls.md) - Keyboard shortcuts
- [Advanced Features](advanced.md) - Dynamic Quality, auto-rotation
- [Configuration](../getting-started/configuration.md) - YAML settings
