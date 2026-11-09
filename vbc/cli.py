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
    from vbc.service.api import run_server
    from vbc.service.bootstrap import ServerConfigError, VbcServer

    if not config.exists():
        typer.echo(f"vbc serve: config file not found: {config}", err=True)
        raise typer.Exit(code=2)
    try:
        server = VbcServer(
            config, demo=demo, demo_config_path=demo_config if demo else None,
            log_file=log_file, host=host, port=port,
        )
    except ServerConfigError as exc:
        typer.echo(f"vbc serve: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:  # pydantic ValidationError included: a setting outside input_dirs
        typer.echo(f"vbc serve: invalid config {config}:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc
    raise typer.Exit(code=run_server(server))


API_OPTION = typer.Option(None, "--api", help="Server URL (default $VBC_API or http://127.0.0.1:8770)")
JSON_OPTION = typer.Option(False, "--json", help="Print the API response as JSON")


@app.command()
def tui(
    api: Optional[str] = API_OPTION,
    log_file: Optional[Path] = typer.Option(
        None, "--log-file", help="Client log file (default ~/.local/state/vbc/tui.log)"),
) -> None:
    """Live dashboard of a running server (keys as in `vbc run`; Ctrl+C detaches, S stops the server)."""
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


@app.command("config")
def show_config(
    api: Optional[str] = API_OPTION,
    as_json: bool = JSON_OPTION,
    push: Optional[Path] = typer.Option(
        None, "--push", exists=True, dir_okay=False,
        help="Send this vbc.yaml to the server (its folders, port and storage are kept); applies after a restart.",
    ),
) -> None:
    """Show the effective server configuration, or push a config file to it."""
    from vbc.clients import commands

    commands.config(api, as_json, push)


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
