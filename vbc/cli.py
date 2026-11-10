"""VBC command line: one Typer app whose subcommands are all peers.

Commands (docs/architecture/service-split.md): ``serve`` (headless server),
``run`` (legacy in-process TUI, today's ``vbc <dirs>``; arguments pass through
unchanged) and the one-shot API commands ``status``, ``threads``, ``refresh``,
``stop``, ``dirs``, ``errors``, ``logs``, ``config``, ``log-level``, and
``tui`` (the Rich dashboard as an API client).

Client commands take ``--api URL`` (default ``$VBC_API`` or
http://127.0.0.1:8770) and ``--json``. Exit status: 0 ok, 1 rejected by the
server, 2 usage error, 3 server unreachable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click
import typer
from typer.core import TyperGroup


class _VbcGroup(TyperGroup):
    """Reject a bare ``vbc <dirs>`` with a hint instead of a generic error."""

    def resolve_command(self, ctx: click.Context, args: list[str]):
        if args and not args[0].startswith("-") and self.get_command(ctx, args[0]) is None:
            typer.echo(
                f"vbc: unknown command {args[0]!r}. To compress directories use: vbc run <dirs>",
                err=True,
            )
            ctx.exit(2)
        return super().resolve_command(ctx, args)


app = typer.Typer(
    cls=_VbcGroup,
    help="VBC (Video Batch Compression).",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """VBC (Video Batch Compression)."""


@app.command()
def serve(
    config: Path = typer.Option(Path("conf/vbc.yaml"), "--config", "-c", help="Path to YAML config"),
    host: Optional[str] = typer.Option(None, help="Listen address (default server.host, 127.0.0.1)"),
    port: Optional[int] = typer.Option(None, help="Listen port (default server.port, 8770)"),
    demo: bool = typer.Option(False, "--demo", help="Simulated processing, no file IO"),
    demo_config: Path = typer.Option(Path("conf/demo.yaml"), "--demo-config", help="Demo YAML config"),
    log_file: Optional[Path] = typer.Option(None, "--log-file", help="Also append logs to this file"),
) -> None:
    """Run the headless server: HTTP API on server.host:server.port, logs on stdout."""
    from vbc.service.__main__ import serve as serve_headless

    raise typer.Exit(serve_headless(config, host, port, demo, demo_config, log_file))


API_OPTION = typer.Option(None, "--api", help="Server URL (default $VBC_API or http://127.0.0.1:8770)")
JSON_OPTION = typer.Option(False, "--json", help="Print the API response as JSON")


@app.command()
def tui(
    api: Optional[str] = API_OPTION,
    log_file: Optional[Path] = typer.Option(
        None, "--log-file", help="Client log file (default ~/.local/state/vbc/tui.log)"),
) -> None:
    """Live dashboard of a running server (keys as in `vbc run`; Ctrl+C detaches, S stops processing; the server keeps running)."""
    from vbc.clients.tui.app import run_tui

    raise typer.Exit(code=run_tui(api, str(log_file) if log_file else None))


@app.command()
def status(
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
    watch: bool = typer.Option(False, "--watch", "-w", help="Keep updating (Ctrl+C to stop)"),
) -> None:
    """Show the server state: run status, counters, active jobs, queue."""
    from vbc.clients import commands

    commands.status(api, as_json, watch)


@app.command()
def threads(
    target: int = typer.Argument(..., help="Number of concurrent jobs (clamped by the server)"),
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Set the number of concurrent jobs (absolute)."""
    from vbc.clients import commands

    commands.threads(api, target, as_json)


@app.command()
def refresh(api: Optional[str] = API_OPTION, as_json: bool = JSON_OPTION) -> None:
    """Rescan the input folders (also resumes after a pause on error)."""
    from vbc.clients import commands

    commands.refresh(api, as_json)


@app.command()
def stop(
    interrupt: bool = typer.Option(False, "--interrupt", help="Interrupt running jobs now"),
    cancel: bool = typer.Option(False, "--cancel", help="Withdraw a pending graceful stop"),
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Stop processing (running jobs finish); the server keeps running, `vbc refresh` resumes."""
    from vbc.clients import commands

    commands.stop(api, interrupt, cancel, as_json)


@app.command("log-level")
def log_level(
    level: str = typer.Argument(..., help="DEBUG, INFO, WARNING or ERROR"),
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Change the server log level at runtime."""
    from vbc.clients import commands

    if level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        typer.echo("vbc log-level: use DEBUG, INFO, WARNING or ERROR", err=True)
        raise typer.Exit(code=2)
    commands.set_log_level(api, level, as_json)


@app.command()
def errors(
    since: int = typer.Option(0, "--since", help="Only entries with a larger id"),
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Show this session's error log (failed files)."""
    from vbc.clients import commands

    commands.errors(api, since, as_json)


@app.command()
def logs(
    since: int = typer.Option(0, "--since", help="Only records with a larger sequence number"),
    level: Optional[str] = typer.Option(None, "--level", help="Minimum level"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing new records"),
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Show the server application log."""
    from vbc.clients import commands

    commands.logs(api, since, level, follow, as_json)


@app.command()
def versions(api: Optional[str] = API_OPTION, as_json: bool = JSON_OPTION) -> None:
    """Versions the server runs with: VBC, ffmpeg and its libraries, exiftool, NVIDIA driver, packages."""
    from vbc.clients import commands

    commands.versions(api, as_json)


config_app = typer.Typer(invoke_without_command=True,
                         help="Show the effective server configuration, change single settings, or push a whole file.")
app.add_typer(config_app, name="config")

RESTART_OPTION = typer.Option(True, "--restart/--no-restart",
                              help="Restart the server now to apply it (running jobs finish first).")


@config_app.callback()
def show_config(
    ctx: typer.Context,
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
    push: Optional[Path] = typer.Option(
        None, "--push", exists=True, dir_okay=False,
        help="Send this vbc.yaml to the server (its folders, port and storage are kept); the server restarts with it.",
    ),
    restart: bool = RESTART_OPTION,
) -> None:
    """Without a subcommand: show the effective configuration, or push a file with --push."""
    if ctx.invoked_subcommand is not None:
        return
    from vbc.clients import commands

    commands.config(api, as_json, push, restart)


@config_app.command("get")
def config_get(
    keys: list[str] = typer.Argument(None, help="Dotted keys or sections, e.g. general.threads (none: all)"),
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Settings as stored in the server's config file (defaults filled in)."""
    from vbc.clients import commands

    commands.config_get(api, keys or [], as_json)


@config_app.command("set")
def config_set(
    assignments: list[str] = typer.Argument(..., help="KEY=VALUE, VALUE in YAML: 40, true, '[mp4, mov]', '{\"GOPR.*\": 180}'"),
    restart: bool = RESTART_OPTION,
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Change single settings. Refused if another client changed the same setting since it was read."""
    from vbc.clients import commands

    commands.config_set(api, assignments, [], restart, as_json)


@config_app.command("unset")
def config_unset(
    keys: list[str] = typer.Argument(..., help="Dotted keys to remove from the file (back to the default)"),
    restart: bool = RESTART_OPTION,
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Remove settings from the config file so their defaults apply."""
    from vbc.clients import commands

    commands.config_set(api, [], keys, restart, as_json)


sources_app = typer.Typer(help="Source files on the server.", no_args_is_help=True)
app.add_typer(sources_app, name="sources")


@sources_app.command("cleanup")
def sources_cleanup(
    delete: bool = typer.Option(False, "--delete", help="Delete the verified duplicates (default: report only)."),
    dirs: list[str] = typer.Option([], "--dir", help="Folder from the Dirs list (repeatable; default: enabled non-manifest folders)."),
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Find sources that already have a verified output in the _out folder; --delete removes them."""
    from vbc.clients import commands

    commands.sources_cleanup(api, as_json, delete, dirs)


manifests_app = typer.Typer(help="Failed manifests on the server.", no_args_is_help=True)
app.add_typer(manifests_app, name="manifests")


@manifests_app.command("restore")
def manifests_restore(
    path: str = typer.Argument(..., help="Failed manifest .json on the server (e.g. in metadata_err/...)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan, move nothing."),
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Put a failed manifest and its archived sources back; the server queues it again."""
    from vbc.clients import commands

    commands.manifests_restore(api, as_json, path, dry_run)


@manifests_app.command("repair")
def manifests_repair(
    path: str = typer.Argument(..., help="Failed .json/.err or a folder with them (server path)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what is repairable, change nothing."),
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Recompress supported failures (FFmpeg 244/-12: video without audio) on the server."""
    from vbc.clients import commands

    commands.manifests_repair(api, as_json, path, dry_run)


dirs_app = typer.Typer(help="Input directories (server paths). Without a subcommand: list them.",
                       invoke_without_command=True, no_args_is_help=False)
app.add_typer(dirs_app, name="dirs")


@dirs_app.callback()
def dirs_root(ctx: typer.Context, api: Optional[str] = API_OPTION, as_json: bool = JSON_OPTION) -> None:
    ctx.obj = {"api": api, "json": as_json}
    if ctx.invoked_subcommand is None:
        from vbc.clients import commands

        commands.dirs_list(api, as_json)


def _dirs_change(ctx: typer.Context, change) -> None:
    from vbc.clients import commands

    commands.dirs_change(ctx.obj["api"], ctx.obj["json"], change)


@dirs_app.command("add")
def dirs_add(ctx: typer.Context, path: str = typer.Argument(..., help="Absolute path on the server")) -> None:
    """Append an enabled directory."""
    from vbc.clients import commands

    _dirs_change(ctx, commands.add_dir(path))


@dirs_app.command("remove")
def dirs_remove(ctx: typer.Context, path: str) -> None:
    """Remove a directory from the list."""
    from vbc.clients import commands

    _dirs_change(ctx, commands.remove_dir(path))


@dirs_app.command("enable")
def dirs_enable(ctx: typer.Context, path: str) -> None:
    """Enable a listed directory."""
    from vbc.clients import commands

    _dirs_change(ctx, commands.set_enabled(path, True))


@dirs_app.command("disable")
def dirs_disable(ctx: typer.Context, path: str) -> None:
    """Disable a listed directory (kept in the list)."""
    from vbc.clients import commands

    _dirs_change(ctx, commands.set_enabled(path, False))


@dirs_app.command("scan")
def dirs_scan(ctx: typer.Context) -> None:
    """Add new media mounts of the server to the list, disabled (enable them after checking)."""
    from vbc.clients import commands

    commands.dirs_scan(ctx.obj["api"], ctx.obj["json"])


@dirs_app.command("set")
def dirs_set(ctx: typer.Context, paths: list[str] = typer.Argument(..., help="New ordered list, all enabled")) -> None:
    """Replace the whole list."""
    from vbc.clients import commands

    _dirs_change(ctx, commands.replace_all(paths))


@app.command(
    add_help_option=False,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(ctx: typer.Context) -> None:
    """Compress directories with the in-process TUI (legacy; see `vbc run --help`)."""
    from vbc_legacy.main import app as legacy_app

    legacy_app(args=ctx.args, prog_name="vbc run")


if __name__ == "__main__":
    app()
