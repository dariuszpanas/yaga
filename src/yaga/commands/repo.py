"""Installed aggregate repository-check commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.errors import InputError, YagaError
from yaga.repository.checker import check_repository
from yaga.repository.models import RepositoryOutputFormat
from yaga.repository.plan import load_repository_check_plan
from yaga.repository.reporting import render_repository_error, render_repository_report

app = typer.Typer(help="Run explicit repository check providers.", no_args_is_help=True)


@app.command("check")
def check_repo(
    plan_path: Annotated[
        Path | None,
        typer.Option(
            "--plan",
            help="Versioned repository check plan. Replaces command-line check selection.",
        ),
    ] = None,
    checks: Annotated[
        list[str] | None,
        typer.Option(
            "--check",
            help=(
                "Provider to run: commit, workflow, workflow-security, or workflow-lint. "
                "Repeat explicitly."
            ),
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
    workflow_security_profile: Annotated[
        str | None,
        typer.Option(
            "--workflow-security-profile",
            help=(
                "Versioned workflow-security profile: recommended-v1 (default), "
                "recommended-v2, or recommended-v3."
            ),
        ),
    ] = None,
    workflow_security_rules: Annotated[
        list[str] | None,
        typer.Option(
            "--workflow-security-rule",
            help="Exact custom workflow-security rule. Repeat explicitly.",
        ),
    ] = None,
    output_format: Annotated[
        RepositoryOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Aggregate report format."),
    ] = RepositoryOutputFormat.TEXT,
) -> None:
    """Run only the explicitly selected providers in canonical order."""
    try:
        selected_checks = checks or ()
        selected_workflow_paths = workflow_paths or ()
        selected_security_profile = workflow_security_profile
        selected_security_rules = workflow_security_rules or ()
        if plan_path is not None:
            if (
                checks
                or workflow_paths
                or workflow_security_profile is not None
                or workflow_security_rules
            ):
                raise InputError(
                    "--plan cannot be combined with --check, --workflow-path, "
                    "--workflow-security-profile, or --workflow-security-rule"
                )
            plan = load_repository_check_plan(plan_path)
            selected_checks = plan.checks
            selected_workflow_paths = tuple(Path(path) for path in plan.workflow_paths)
            selected_security_profile = plan.workflow_security_profile
            selected_security_rules = plan.workflow_security_rules

        report = check_repository(
            repository,
            selected_checks,
            config=config,
            commit=commit,
            revision_range=revision_range,
            workflow_paths=selected_workflow_paths,
            workflow_security_profile=selected_security_profile,
            workflow_security_rules=selected_security_rules,
        )
    except YagaError as error:
        typer.echo(render_repository_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error

    typer.echo(render_repository_report(report, output_format), err=bool(report.errored))
    if report.errored:
        raise typer.Exit(code=2)
    if report.failed:
        raise typer.Exit(code=1)
