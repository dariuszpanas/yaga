"""Installed command for explicit committed entry-mode policy."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.errors import YagaError
from yaga.modes.reporting import ModeOutputFormat, render_mode_error, render_mode_report
from yaga.modes.service import check_mode_policy

app = typer.Typer(help="Check committed entry-mode policy.", no_args_is_help=True)


@app.command("check")
def check_committed_mode_policy(
    policy_path: Annotated[
        Path,
        typer.Option("--policy", help="Explicit versioned committed-mode policy file."),
    ],
    revision: Annotated[
        str,
        typer.Option("--revision", help="Exact Git revision whose committed modes are checked."),
    ],
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Git repository containing the revision."),
    ] = Path("."),
    output_format: Annotated[
        ModeOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = ModeOutputFormat.TEXT,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress policy reports and use only the exit code."),
    ] = False,
) -> None:
    """Check one exact committed Git tree against one explicit mode policy."""
    try:
        checked = check_mode_policy(
            repository,
            policy_path=policy_path,
            revision=revision,
        )
    except YagaError as error:
        typer.echo(render_mode_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error

    if not quiet:
        typer.echo(render_mode_report(checked, output_format))
    if not checked.report.valid:
        raise typer.Exit(code=1)
