"""Installed Typer command tree for YAGA."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

import typer

from yaga.commands.commit import app as commit_app
from yaga.commands.config import app as config_app
from yaga.commands.gate import app as gate_app

app = typer.Typer(
    name="yaga",
    help="Extensible, configurable developer workflow automation.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(commit_app, name="commit")
app.add_typer(config_app, name="config")
app.add_typer(gate_app, name="gate")


def _version_callback(value: bool) -> None:
    if not value:
        return
    try:
        installed_version = version("yaga-cli")
    except PackageNotFoundError:
        installed_version = "0+unknown"
    typer.echo(f"yaga {installed_version}")
    raise typer.Exit()


@app.callback()
def root(
    version_requested: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed YAGA version and exit.",
        ),
    ] = False,
) -> None:
    """Run YAGA developer workflow tools."""
