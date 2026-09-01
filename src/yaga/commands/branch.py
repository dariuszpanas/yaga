"""Installed command for explicit branch-name policy."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.branches.reporting import (
    BranchOutputFormat,
    render_branch_error,
    render_branch_report,
)
from yaga.branches.service import check_branch
from yaga.errors import YagaError

app = typer.Typer(help="Check branch-name policy.", no_args_is_help=True)


@app.command("check")
def check_branch_policy(
    policy_path: Annotated[
        Path,
        typer.Option("--policy", help="Explicit versioned branch-name policy file."),
    ],
    branch: Annotated[
        str,
        typer.Option("--name", help="Exact short branch name to check."),
    ],
    output_format: Annotated[
        BranchOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = BranchOutputFormat.TEXT,
) -> None:
    """Check one explicit short branch name against one explicit policy."""
    try:
        checked = check_branch(policy_path=policy_path, branch=branch)
    except YagaError as error:
        typer.echo(render_branch_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error

    typer.echo(render_branch_report(checked, output_format))
    if not checked.report.valid:
        raise typer.Exit(code=1)
