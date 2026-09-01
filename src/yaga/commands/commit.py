"""Installed commands for Conventional Commit policy."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.commits.checker import check_target
from yaga.commits.config import load_config
from yaga.commits.git import read_commit, read_range
from yaga.commits.models import CommitTarget, OutputFormat, ValidationReport
from yaga.commits.reporting import render_error, render_report
from yaga.commits.sources import from_file, from_message, from_stdin
from yaga.errors import InputError, YagaError

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
        selected = [
            message is not None,
            file_ is not None,
            stdin,
            commit is not None,
            revision_range is not None,
        ]
        if sum(selected) > 1:
            raise InputError("choose only one of --message, --file, --stdin, --commit, or --range")
        repo = repository.expanduser().resolve()
        loaded = load_config(config, start=repo)
        targets = _select_targets(
            message=message,
            file_=file_,
            stdin=stdin,
            commit=commit,
            revision_range=revision_range,
            repository=repo,
            max_commits=loaded.policy.max_commits,
        )
        results = tuple(check_target(target, loaded.policy) for target in targets)
        report = ValidationReport(results=results, config_path=loaded.path)
    except YagaError as error:
        typer.echo(render_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error

    if not quiet:
        typer.echo(render_report(report, output_format))
    if not report.valid:
        raise typer.Exit(code=1)


def _select_targets(
    *,
    message: str | None,
    file_: Path | None,
    stdin: bool,
    commit: str | None,
    revision_range: str | None,
    repository: Path,
    max_commits: int,
) -> list[CommitTarget]:
    if message is not None:
        return [from_message(message)]
    if file_ is not None:
        return [from_file(file_)]
    if stdin:
        return [from_stdin()]
    if revision_range is not None:
        return read_range(repository, revision_range, max_commits=max_commits)
    return [read_commit(repository, commit or "HEAD")]
