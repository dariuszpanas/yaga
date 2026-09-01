"""Installed command for explicit committed-tree policy."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.errors import YagaError
from yaga.trees.reporting import TreeOutputFormat, render_tree_error, render_tree_report
from yaga.trees.service import check_tree_policy

app = typer.Typer(help="Check committed-tree path policy.", no_args_is_help=True)


@app.command("check")
def check_committed_tree_policy(
    policy_path: Annotated[
        Path,
        typer.Option("--policy", help="Explicit versioned committed-tree policy file."),
    ],
    revision: Annotated[
        str,
        typer.Option("--revision", help="Exact Git revision whose committed tree is checked."),
    ],
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Git repository containing the revision."),
    ] = Path("."),
    output_format: Annotated[
        TreeOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = TreeOutputFormat.TEXT,
) -> None:
    """Check one exact committed Git tree against one explicit policy."""
    try:
        checked = check_tree_policy(
            repository,
            policy_path=policy_path,
            revision=revision,
        )
    except YagaError as error:
        typer.echo(render_tree_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error

    typer.echo(render_tree_report(checked, output_format))
    if not checked.report.valid:
        raise typer.Exit(code=1)
