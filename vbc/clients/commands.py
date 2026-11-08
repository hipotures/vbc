"""One-shot client commands (`vbc status`, `vbc threads`, ...).

Human-readable output by default, ``--json`` prints the API DTOs as-is.
Exit status: 0 ok, 1 the server rejected the request (HTTP 4xx/5xx),
3 the server could not be reached.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Callable, List, Optional

import typer
from pydantic import BaseModel

from vbc import contracts as dto
from vbc.clients import format as fmt
from vbc.clients.api_client import ApiClient, ApiError, ApiUnavailable, extrapolated_percent, replace_dirs

EXIT_REJECTED = 1
EXIT_UNREACHABLE = 3


def run(api: Optional[str], action: Callable[[ApiClient], None]) -> None:
    """Run ``action`` with a client; map API failures to exit codes."""
    try:
        with ApiClient(api) as client:
            action(client)
    except ApiUnavailable as exc:
        typer.echo(f"vbc: server unreachable: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNREACHABLE) from exc
    except ApiError as exc:
        typer.echo(f"vbc: {exc}", err=True)
        if exc.problem is not None:
            if exc.problem.detail:
                typer.echo(f"  {exc.problem.detail}", err=True)
            for error in exc.problem.errors:
                typer.echo(f"  - {error}", err=True)
        raise typer.Exit(code=EXIT_REJECTED) from exc


def emit_json(value: BaseModel | list) -> None:
    if isinstance(value, list):
        typer.echo(json.dumps([v.model_dump(mode="json") for v in value], indent=2))
    else:
        typer.echo(value.model_dump_json(indent=2))


# ── status ──────────────────────────────────────────────────────────────────


def render_status(snapshot: dto.StateSnapshot, client: ApiClient) -> str:
    now = client.server_now_ms()
    c, d = snapshot.counters, snapshot.discovery
    t = snapshot.threads
    lines = [f"VBC {client.base_url}  {snapshot.run_status.upper()}  threads {t.current} ({t.minimum}-{t.maximum})"]
    if snapshot.status_message:
        lines.append(f"  ! {snapshot.status_message}")
    if snapshot.shutdown_requested and snapshot.run_status != "shutting_down":
        lines.append("  shutdown requested")
    done_since = c.completed - c.completed_at_last_discovery
    lines.append(
        f"Files: {done_since}/{d.files_to_process} done this pass, found {d.files_found}, "
        f"already compressed {d.already_compressed}, ignored small {d.ignored_small}, "
        f"ignored err {d.ignored_err}"
    )
    lines.append(
        f"Session: completed {c.completed}, failed {c.failed}, skipped {c.skipped}, "
        f"hw_cap {c.hw_cap}, camera skip {c.cam_skipped}, kept original {c.min_ratio_skip}, "
        f"interrupted {c.interrupted}"
    )
    saved = snapshot.total_input_bytes - snapshot.total_output_bytes
    elapsed = (now - snapshot.processing_started_at) / 1000 if snapshot.processing_started_at else None
    eta = max(0, (snapshot.eta_at - now) / 1000) if snapshot.eta_at else None
    lines.append(
        f"Bytes: in {fmt.size(snapshot.total_input_bytes)}, out {fmt.size(snapshot.total_output_bytes)}, "
        f"saved {fmt.size(saved)} | {fmt.rate(snapshot.throughput.bytes_per_s if snapshot.throughput else None)} | "
        f"elapsed {fmt.duration(elapsed)} | ETA {fmt.duration(eta)}"
    )
    if snapshot.repair.active or snapshot.repair.candidates:
        r = snapshot.repair
        lines.append(f"Repair: {'running, ' if r.active else ''}{r.repaired}/{r.candidates} repaired")
    lines.append(f"Active ({len(snapshot.active_jobs)}):")
    for job in snapshot.active_jobs:
        pct = extrapolated_percent(job.progress, now) if job.progress else None
        pct_text = f"{pct:5.1f}%" if pct is not None else "  ...  "
        parts = [pct_text, job.name, fmt.size(job.input_bytes), job.quality,
                 fmt.duration((now - job.started_at) / 1000)]
        lines.append("  " + "  ".join(p for p in parts if p))
    lines.append(f"Queue: {snapshot.queue.count} files, {fmt.size(snapshot.queue.bytes)}")
    if snapshot.recent_jobs:
        lines.append("Recent:")
        for job in snapshot.recent_jobs[:5]:
            outcome = job.status.lower()
            extra = f" -> {fmt.size(job.output_bytes)}" if job.output_bytes else ""
            note = f"  ({job.error_message})" if job.error_message else ""
            lines.append(f"  {outcome:<11} {job.name}  {fmt.size(job.input_bytes)}{extra}{note}")
    if snapshot.errors_count:
        lines.append(f"Errors: {snapshot.errors_count} (vbc errors)")
    if snapshot.last_action:
        lines.append(f"Last: {snapshot.last_action.text} ({fmt.ago((now - snapshot.last_action.at) / 1000)})")
    return "\n".join(lines)


def status(api: Optional[str], as_json: bool, watch: bool) -> None:
    def action(client: ApiClient) -> None:
        snapshot = client.state()
        if not watch:
            emit_json(snapshot) if as_json else typer.echo(render_status(snapshot, client))
            return
        interactive = sys.stdout.isatty() and not as_json
        last_render = 0.0
        while True:
            if as_json:
                typer.echo(snapshot.model_dump_json())
            elif interactive:
                typer.echo("\x1b[H\x1b[2J" + render_status(snapshot, client))
            else:
                typer.echo(render_status(snapshot, client) + "\n")
            last_render = time.monotonic()
            while True:
                # Interactive: redraw every second (extrapolated progress); otherwise only on change.
                wait = 1.0 if interactive else 25.0
                newer = client.state(since=snapshot.rev, wait=wait)
                if newer is not None:
                    snapshot = newer
                    break
                if interactive and time.monotonic() - last_render >= 1.0:
                    break

    try:
        run(api, action)
    except KeyboardInterrupt:
        raise typer.Exit(code=0)


# ── commands ────────────────────────────────────────────────────────────────


def threads(api: Optional[str], target: int, as_json: bool) -> None:
    def action(client: ApiClient) -> None:
        result = client.set_threads(target)
        if as_json:
            emit_json(result)
        else:
            note = "" if result.current == target else f" (requested {target}, limits {result.minimum}-{result.maximum})"
            typer.echo(f"threads: {result.current}{note}")

    run(api, action)


def refresh(api: Optional[str], as_json: bool) -> None:
    def action(client: ApiClient) -> None:
        ack = client.refresh()
        emit_json(ack) if as_json else typer.echo("refresh requested")

    run(api, action)


def shutdown(api: Optional[str], interrupt: bool, cancel: bool, as_json: bool) -> None:
    if interrupt and cancel:
        typer.echo("vbc shutdown: --interrupt and --cancel exclude each other", err=True)
        raise typer.Exit(code=2)

    def action(client: ApiClient) -> None:
        if cancel:
            ack, text = client.cancel_shutdown(), "shutdown cancelled"
        elif interrupt:
            ack, text = client.shutdown("interrupt"), "interrupt requested: running jobs are stopped"
        else:
            ack, text = client.shutdown("graceful"), "graceful shutdown requested: running jobs finish first"
        emit_json(ack) if as_json else typer.echo(text)

    run(api, action)


def set_log_level(api: Optional[str], level: str, as_json: bool) -> None:
    def action(client: ApiClient) -> None:
        ack = client.set_log_level(level)
        emit_json(ack) if as_json else typer.echo(f"log level: {level.upper()}")

    run(api, action)


# ── dirs ────────────────────────────────────────────────────────────────────


def render_dirs(state: dto.DirsState) -> str:
    if not state.entries:
        return f"no input directories (dirs_rev {state.dirs_rev})"
    lines = [f"input directories (dirs_rev {state.dirs_rev}), server paths:"]
    for index, entry in enumerate(state.entries, start=1):
        flag = "on " if entry.enabled else "off"
        health = "" if entry.exists and entry.readable else ("  [missing]" if not entry.exists else "  [no access]")
        lines.append(f"  {index:>2}. [{flag}] {entry.path}{health}")
    return "\n".join(lines)


def dirs_list(api: Optional[str], as_json: bool) -> None:
    def action(client: ApiClient) -> None:
        state = client.dirs()
        emit_json(state) if as_json else typer.echo(render_dirs(state))

    run(api, action)


def dirs_change(api: Optional[str], as_json: bool, change: Callable[[List[dto.DirEntry]], List[dto.DirEntry]]) -> None:
    """Read, edit locally, send the whole ordered list with If-Match."""

    def action(client: ApiClient) -> None:
        current = client.dirs()
        entries = change(replace_dirs(current.entries))
        state = client.put_dirs(entries, current.dirs_rev)
        emit_json(state) if as_json else typer.echo(render_dirs(state))

    run(api, action)


def _find(entries: List[dto.DirEntry], path: str) -> int:
    normalized = path.rstrip("/") or "/"
    for index, entry in enumerate(entries):
        if (entry.path.rstrip("/") or "/") == normalized:
            return index
    typer.echo(f"vbc dirs: {path} is not in the list", err=True)
    raise typer.Exit(code=2)


def add_dir(path: str):
    def change(entries: List[dto.DirEntry]) -> List[dto.DirEntry]:
        return entries + [dto.DirEntry(path=path, enabled=True)]
    return change


def remove_dir(path: str):
    def change(entries: List[dto.DirEntry]) -> List[dto.DirEntry]:
        index = _find(entries, path)
        return entries[:index] + entries[index + 1:]
    return change


def set_enabled(path: str, enabled: bool):
    def change(entries: List[dto.DirEntry]) -> List[dto.DirEntry]:
        index = _find(entries, path)
        entries[index] = dto.DirEntry(path=entries[index].path, enabled=enabled)
        return entries
    return change


def replace_all(paths: List[str]):
    def change(_entries: List[dto.DirEntry]) -> List[dto.DirEntry]:
        return [dto.DirEntry(path=p, enabled=True) for p in paths]
    return change


# ── errors / logs / config ──────────────────────────────────────────────────


def render_error(entry: dto.ErrorEntry) -> str:
    meta = [fmt.size(entry.size_bytes)]
    if entry.width and entry.height:
        meta.append(f"{entry.width}x{entry.height}")
    if entry.fps:
        meta.append(f"{entry.fps:g}fps")
    if entry.codec:
        meta.append(entry.codec)
    if entry.duration_s:
        meta.append(fmt.duration(entry.duration_s))
    stage = f"[{entry.stage}] " if entry.stage else ""
    code = f" (exit {entry.exit_code})" if entry.exit_code is not None else ""
    return f"#{entry.id} {stage}{entry.path} • {' • '.join(meta)}\n    {entry.error_message}{code}"


def errors(api: Optional[str], since: int, as_json: bool) -> None:
    def action(client: ApiClient) -> None:
        page = client.errors(since=since)
        if as_json:
            emit_json(page)
            return
        if not page.entries:
            typer.echo("no errors in this session")
        for entry in page.entries:
            typer.echo(render_error(entry))

    run(api, action)


def logs(api: Optional[str], since: int, level: Optional[str], follow: bool, as_json: bool) -> None:
    def show(entries: List[dto.LogEntry]) -> None:
        for entry in entries:
            if as_json:
                typer.echo(entry.model_dump_json())
            else:
                stamp = time.strftime("%H:%M:%S", time.localtime(entry.ts / 1000))
                typer.echo(f"{stamp} {entry.level:<7} [{entry.logger}] {entry.message}")

    def action(client: ApiClient) -> None:
        page = client.logs(since=since, level=level)
        show(page.entries)
        cursor = page.entries[-1].seq if page.entries else page.last_seq
        while follow:
            page = client.logs(since=cursor, level=level, wait=25)
            show(page.entries)
            if page.entries:
                cursor = page.entries[-1].seq
            elif page.last_seq > cursor:
                cursor = page.last_seq  # newer records filtered out by level

    try:
        run(api, action)
    except KeyboardInterrupt:
        raise typer.Exit(code=0)


def config(api: Optional[str], as_json: bool) -> None:
    def action(client: ApiClient) -> None:
        view = client.config()
        if as_json:
            emit_json(view)
            return
        for name, section in view.sections.items():
            typer.echo(f"[{name}]")
            for key, value in section.items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(value)
                typer.echo(f"  {key}: {value}")

    run(api, action)
