"""VBC command line: one Typer app whose subcommands are all peers.

Planned commands (docs/architecture/service-split.md): ``serve``, ``tui``,
``run`` and the one-shot API commands (``status``, ``threads``, ...). For now
only ``run`` exists; it hands its arguments unchanged to the legacy
application (today's ``vbc <dirs>``).
"""

from __future__ import annotations

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


@app.command(
    add_help_option=False,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(ctx: typer.Context) -> None:
    """Compress directories with the in-process TUI (legacy; see `vbc run --help`)."""
    from vbc_legacy.main import app as legacy_app

    legacy_app(args=ctx.args, prog_name="vbc run")
