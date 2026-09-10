"""Installed command for explicit committed-path portability policy."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.errors import YagaError
from yaga.paths.reporting import PathOutputFormat, render_path_error, render_path_report
from yaga.paths.service import check_path_policy

app = typer.Typer(help="Check committed-path portability policy.", no_args_is_help=True)


@app.command("check")
def check_committed_path_policy(
    policy_path: Annotated[
        Path,
        typer.Option("--policy", help="Explicit versioned committed-path policy file."),
    ],
    revision: Annotated[
        str,
        typer.Option("--revision", help="Exact Git revision whose committed paths are checked."),
    ],
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Git repository containing the revision."),
    ] = Path("."),
    output_format: Annotated[
        PathOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = PathOutputFormat.TEXT,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress policy reports and use only the exit code."),
    ] = False,
) -> None:
    """Check one exact committed Git tree against one explicit path policy."""
    try:
        checked = check_path_policy(
            repository,
            policy_path=policy_path,
            revision=revision,
        )
    except YagaError as error:
        typer.echo(render_path_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error

    if not quiet:
        typer.echo(render_path_report(checked, output_format))
    if not checked.report.valid:
        raise typer.Exit(code=1)
