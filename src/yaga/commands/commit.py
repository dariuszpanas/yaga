"""Installed commands for Conventional Commit policy."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.commits.models import OutputFormat
from yaga.commits.quality import (
    DEFAULT_LOW_QUALITY_THRESHOLD,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_REVISION,
)
from yaga.commits.reporting import render_error, render_quality_report, render_report
from yaga.commits.service import check_commit_quality
from yaga.commits.service import check_commits as check_commit_service
from yaga.errors import YagaError

app = typer.Typer(help="Inspect and enforce commit-message policy.", no_args_is_help=True)


@app.command("check")
def check_commits(
    message: Annotated[
        str | None,
        typer.Option("--message", "-m", help="Check one explicit message."),
    ] = None,
    file_: Annotated[
        Path | None,
        typer.Option("--file", "-F", help="Read one UTF-8 message file."),
    ] = None,
    stdin: Annotated[
        bool,
        typer.Option("--stdin", help="Read one message from standard input."),
    ] = False,
    commit: Annotated[
        str | None,
        typer.Option("--commit", "-c", help="Check one Git revision."),
    ] = None,
    revision_range: Annotated[
        str | None,
        typer.Option("--range", "-r", help="Check a Git revision range oldest-first."),
    ] = None,
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Git repository and configuration discovery root."),
    ] = Path("."),
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Use one explicit pyproject.toml or .yaga.toml."),
    ] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = OutputFormat.TEXT,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress reports and use only the exit code."),
    ] = False,
) -> None:
    """Check HEAD or exactly one explicitly selected message source."""
    try:
        report = check_commit_service(
            repository,
            message=message,
            file=file_,
            stdin=stdin,
            commit=commit,
            revision_range=revision_range,
            config=config,
        )
    except YagaError as error:
        typer.echo(render_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error

    if not quiet:
        typer.echo(render_report(report, output_format))
    if not report.valid:
        raise typer.Exit(code=1)


@app.command("quality")
def quality_check(
    message: Annotated[
        str | None, typer.Option("--message", "-m", help="Check one explicit message.")
    ] = None,
    file_: Annotated[
        Path | None, typer.Option("--file", "-F", help="Read one UTF-8 message file.")
    ] = None,
    stdin: Annotated[
        bool, typer.Option("--stdin", help="Read one message from standard input.")
    ] = False,
    commit: Annotated[
        str | None, typer.Option("--commit", "-c", help="Check one Git revision.")
    ] = None,
    revision_range: Annotated[
        str | None, typer.Option("--range", "-r", help="Check a Git revision range oldest-first.")
    ] = None,
    repository: Annotated[Path, typer.Option("--repo", help="Git repository root.")] = Path("."),
    model_id: Annotated[
        str, typer.Option("--model", help="Hugging Face model identifier.")
    ] = DEFAULT_MODEL_ID,
    revision: Annotated[
        str, typer.Option("--revision", help="Pinned lowercase model revision SHA.")
    ] = DEFAULT_MODEL_REVISION,
    threshold: Annotated[
        float,
        typer.Option(
            "--threshold", min=0.0, max=1.0, help="Low-quality probability that flags a message."
        ),
    ] = DEFAULT_LOW_QUALITY_THRESHOLD,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use only the local Hugging Face cache.")
    ] = False,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", case_sensitive=False, help="Report format.")
    ] = OutputFormat.TEXT,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress reports and use only the exit code.")
    ] = False,
) -> None:
    """Advisory-check commit-message quality with an optional local model."""
    try:
        report = check_commit_quality(
            repository,
            message=message,
            file=file_,
            stdin=stdin,
            commit=commit,
            revision_range=revision_range,
            model_id=model_id,
            revision=revision,
            threshold=threshold,
            offline=offline,
        )
    except YagaError as error:
        typer.echo(render_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    if not quiet:
        typer.echo(render_quality_report(report, output_format))
    if not report.valid:
        raise typer.Exit(code=1)
