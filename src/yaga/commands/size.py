"""Installed command for explicit committed blob-size policy."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.errors import YagaError
from yaga.sizes.reporting import SizeOutputFormat, render_size_error, render_size_report
from yaga.sizes.service import check_size_policy

app = typer.Typer(help="Check committed blob-size policy.", no_args_is_help=True)


@app.command("check")
def check_committed_size_policy(
    policy_path: Annotated[
        Path,
        typer.Option("--policy", help="Explicit versioned committed blob-size policy file."),
    ],
    revision: Annotated[
        str,
        typer.Option("--revision", help="Exact Git revision whose committed blobs are checked."),
    ],
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Git repository containing the revision."),
    ] = Path("."),
    output_format: Annotated[
        SizeOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = SizeOutputFormat.TEXT,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress policy reports and use only the exit code."),
    ] = False,
) -> None:
    """Check one exact committed Git tree against one explicit size policy."""
    try:
        checked = check_size_policy(
            repository,
            policy_path=policy_path,
            revision=revision,
        )
    except YagaError as error:
        typer.echo(render_size_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error

    if not quiet:
        typer.echo(render_size_report(checked, output_format))
    if not checked.report.valid:
        raise typer.Exit(code=1)
