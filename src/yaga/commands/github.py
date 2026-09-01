"""Installed commands for read-only GitHub event checks."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.commits.github_event import check_pull_request
from yaga.commits.github_reporting import (
    PullRequestOutputFormat,
    render_pull_request_error,
    render_pull_request_report,
)
from yaga.errors import YagaError

app = typer.Typer(
    help="Check untrusted GitHub event data without API access.", no_args_is_help=True
)
pull_request_app = typer.Typer(help="Inspect one pull-request event.", no_args_is_help=True)
app.add_typer(pull_request_app, name="pull-request")


@pull_request_app.command("check")
def check_pull_request_event(
    event_file: Annotated[
        Path,
        typer.Option("--event-file", help="Read one GitHub pull_request event JSON file."),
    ],
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Git repository and configuration discovery root."),
    ] = Path("."),
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Use one explicit pyproject.toml or .yaga.toml."),
    ] = None,
    output_format: Annotated[
        PullRequestOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = PullRequestOutputFormat.TEXT,
) -> None:
    """Check the PR title and exact event base-to-head commit range."""
    try:
        report = check_pull_request(
            event_file,
            repository,
            config=config,
        )
    except YagaError as error:
        typer.echo(render_pull_request_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(render_pull_request_report(report, output_format))
    if not report.valid:
        raise typer.Exit(code=1)
