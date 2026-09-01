"""Installed commands for GitHub workflow policy."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.errors import YagaError
from yaga.workflows.checker import check_workflows
from yaga.workflows.lint import lint_workflows
from yaga.workflows.models import WorkflowOutputFormat
from yaga.workflows.reporting import (
    render_workflow_error,
    render_workflow_lint_error,
    render_workflow_lint_report,
    render_workflow_report,
)
from yaga.workflows.security import check_workflow_security
from yaga.workflows.security_reporting import (
    render_workflow_security_error,
    render_workflow_security_report,
)

app = typer.Typer(help="Inspect GitHub workflows.", no_args_is_help=True)


@app.command("check")
def check_workflow_references(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help=("Workflow files or direct-child directories. Defaults to .github/workflows.")
        ),
    ] = None,
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Repository root for workflow path resolution."),
    ] = Path("."),
    output_format: Annotated[
        WorkflowOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = WorkflowOutputFormat.TEXT,
) -> None:
    """Require immutable external references in selected GitHub workflows."""
    try:
        report = check_workflows(repository, paths or ())
    except YagaError as error:
        typer.echo(render_workflow_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(render_workflow_report(report, output_format))
    if not report.valid:
        raise typer.Exit(code=1)


@app.command("security")
def check_workflow_security_policy(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help=("Workflow files or direct-child directories. Defaults to .github/workflows.")
        ),
    ] = None,
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Repository root for workflow path resolution."),
    ] = Path("."),
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help=("Versioned rule profile: recommended-v1 (default) or recommended-v2."),
        ),
    ] = None,
    rules: Annotated[
        list[str] | None,
        typer.Option(
            "--rule",
            help="Exact custom rule to run instead of a profile. Repeat explicitly.",
        ),
    ] = None,
    output_format: Annotated[
        WorkflowOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = WorkflowOutputFormat.TEXT,
) -> None:
    """Enforce a bounded GitHub Actions trust policy."""
    try:
        report = check_workflow_security(
            repository,
            paths or (),
            profile=profile,
            rules=rules or (),
        )
    except YagaError as error:
        typer.echo(render_workflow_security_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(render_workflow_security_report(report, output_format))
    if not report.valid:
        raise typer.Exit(code=1)


@app.command("lint")
def lint_workflow_syntax(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help=("Workflow files or direct-child directories. Defaults to .github/workflows.")
        ),
    ] = None,
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Repository root for workflow path resolution."),
    ] = Path("."),
    output_format: Annotated[
        WorkflowOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = WorkflowOutputFormat.TEXT,
) -> None:
    """Lint selected workflows with YAGA's pinned actionlint container."""
    try:
        report = lint_workflows(repository, paths or ())
    except YagaError as error:
        typer.echo(render_workflow_lint_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(render_workflow_lint_report(report, output_format))
    if not report.valid:
        raise typer.Exit(code=1)
