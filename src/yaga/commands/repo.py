"""Installed aggregate repository-check commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.errors import YagaError
from yaga.repository.checker import check_repository
from yaga.repository.models import RepositoryOutputFormat
from yaga.repository.reporting import render_repository_error, render_repository_report

app = typer.Typer(help="Run explicit repository check providers.", no_args_is_help=True)


@app.command("check")
def check_repo(
    checks: Annotated[
        list[str] | None,
        typer.Option(
            "--check",
            help="Provider to run: commit, workflow, or workflow-lint. Repeat explicitly.",
        ),
    ] = None,
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Repository and configuration discovery root."),
    ] = Path("."),
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Commit provider configuration file."),
    ] = None,
    commit: Annotated[
        str | None,
        typer.Option("--commit", "-c", help="Commit provider Git revision. Defaults to HEAD."),
    ] = None,
    revision_range: Annotated[
        str | None,
        typer.Option("--range", "-r", help="Commit provider Git range, oldest-first."),
    ] = None,
    workflow_paths: Annotated[
        list[Path] | None,
        typer.Option(
            "--workflow-path",
            help="Workflow file or direct-child directory. Repeat for multiple selections.",
        ),
    ] = None,
    output_format: Annotated[
        RepositoryOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Aggregate report format."),
    ] = RepositoryOutputFormat.TEXT,
) -> None:
    """Run only the explicitly selected providers in canonical order."""
    try:
        report = check_repository(
            repository,
            checks or (),
            config=config,
            commit=commit,
            revision_range=revision_range,
            workflow_paths=workflow_paths or (),
        )
    except YagaError as error:
        typer.echo(render_repository_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error

    typer.echo(render_repository_report(report, output_format), err=bool(report.errored))
    if report.errored:
        raise typer.Exit(code=2)
    if report.failed:
        raise typer.Exit(code=1)
