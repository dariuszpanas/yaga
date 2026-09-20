"""Installed commands for configuration inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.commits.config import load_config
from yaga.commits.config_init import ConfigStarter, initialize_config
from yaga.commits.models import OutputFormat
from yaga.commits.reporting import render_config, render_error
from yaga.errors import YagaError

app = typer.Typer(help="Inspect YAGA configuration.", no_args_is_help=True)


@app.command("init")
def init_config(
    starter: Annotated[
        ConfigStarter, typer.Option("--starter", help="Frozen editable starter policy.")
    ] = ConfigStarter.RECOMMENDED_V1,
    repository: Annotated[
        Path,
        typer.Option(
            "--repo",
            help="Directory that will receive a standalone .yaga.toml.",
        ),
    ] = Path("."),
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = OutputFormat.TEXT,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Validate and report the starter policy without creating .yaga.toml.",
        ),
    ] = False,
) -> None:
    """Create or preview a recommended standalone .yaga.toml without overwriting."""
    try:
        loaded = initialize_config(repository, dry_run=dry_run, starter=starter)
    except YagaError as error:
        typer.echo(render_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(render_config(loaded, output_format, dry_run=dry_run))


@app.command("show")
def show_config(
    commit_type: Annotated[
        str | None,
        typer.Option("--type", help="Explain body and scope requirements for this type."),
    ] = None,
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
        rendered = render_config(loaded, output_format, commit_type=commit_type)
    except YagaError as error:
        typer.echo(render_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(rendered)
