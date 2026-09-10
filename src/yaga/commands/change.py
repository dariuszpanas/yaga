"""Installed command for changed-path coupling policy."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.changes.reporting import (
    ChangeOutputFormat,
    render_change_error,
    render_change_report,
)
from yaga.changes.service import check_changes
from yaga.errors import YagaError

app = typer.Typer(help="Check changed-path coupling policy.", no_args_is_help=True)


@app.command("check")
def check_change_policy(
    policy_path: Annotated[
        Path,
        typer.Option("--policy", help="Explicit versioned changed-path policy file."),
    ],
    revision_range: Annotated[
        str,
        typer.Option("--range", help="Exact A..B or A...B Git range."),
    ],
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Git repository containing both range endpoints."),
    ] = Path("."),
    output_format: Annotated[
        ChangeOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = ChangeOutputFormat.TEXT,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress policy reports and use only the exit code."),
    ] = False,
) -> None:
    """Require configured path changes for one explicit Git range."""
    try:
        checked = check_changes(
            repository,
            policy_path=policy_path,
            revision_range=revision_range,
        )
    except YagaError as error:
        typer.echo(render_change_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error

    if not quiet:
        typer.echo(render_change_report(checked, output_format))
    if not checked.report.valid:
        raise typer.Exit(code=1)
