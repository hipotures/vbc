"""VBC command line: one Typer app whose subcommands are all peers.

Commands (docs/architecture/service-split.md): ``serve`` (headless server),
``run`` (legacy in-process TUI, today's ``vbc <dirs>``; arguments pass through
unchanged). Planned: ``tui`` and the one-shot API commands (``status``,
``threads``, ...).
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
    raise typer.Exit(code=run_server(server))


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
