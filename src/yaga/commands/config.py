"""Installed commands for configuration inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.commits.config import load_config
from yaga.commits.models import OutputFormat
from yaga.commits.reporting import render_config, render_error
from yaga.errors import YagaError

app = typer.Typer(help="Inspect YAGA configuration.", no_args_is_help=True)


@app.command("show")
def show_config(
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Configuration discovery root."),
    ] = Path("."),
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Use one explicit pyproject.toml or .yaga.toml."),
    ] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = OutputFormat.TEXT,
) -> None:
    """Show the effective configuration and its source."""
    try:
        loaded = load_config(config, start=repository.expanduser().resolve())
    except YagaError as error:
        typer.echo(render_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(render_config(loaded, output_format))
