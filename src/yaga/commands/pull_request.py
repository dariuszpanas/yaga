"""Installed commands for explicit pull-request description checks."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.descriptions.reporting import (
    DescriptionOutputFormat,
    render_description_error,
    render_description_report,
)
from yaga.descriptions.service import check_description_source
from yaga.errors import YagaError

app = typer.Typer(help="Check pull-request content.", no_args_is_help=True)
description_app = typer.Typer(help="Check a Markdown PR description.", no_args_is_help=True)
github_description_app = typer.Typer(
    help="Check the description in one explicit GitHub event.", no_args_is_help=True
)
app.add_typer(description_app, name="description")

PolicyOption = Annotated[Path, typer.Option("--policy", help="Explicit description policy TOML.")]
TemplateOption = Annotated[
    Path | None,
    typer.Option("--template", help="Explicit Markdown template with required headings."),
]
FormatOption = Annotated[
    DescriptionOutputFormat,
    typer.Option("--format", case_sensitive=False, help="Report format."),
]
QuietOption = Annotated[
    bool, typer.Option("--quiet", "-q", help="Suppress reports; errors still appear on stderr.")
]


@description_app.command("check")
def check_local_description(
    policy_path: PolicyOption,
    file_path: Annotated[
        Path | None,
        typer.Option("--file", help="UTF-8 description file; exclusive with --stdin and --pr."),
    ] = None,
    stdin: Annotated[
        bool, typer.Option("--stdin", help="Read description from standard input.")
    ] = False,
    pull_request: Annotated[
        str | None,
        typer.Option(
            "--pr", help="Fetch the current description with gh: PR URL or positive number."
        ),
    ] = None,
    github_repository: Annotated[
        str | None,
        typer.Option("--repo", help="[HOST/]OWNER/REPO; required only with a numeric --pr."),
    ] = None,
    template_path: TemplateOption = None,
    output_format: FormatOption = DescriptionOutputFormat.TEXT,
    quiet: QuietOption = False,
) -> None:
    """Validate exactly one explicit Markdown source against one explicit policy."""
    _run(
        policy_path=policy_path,
        file_path=file_path,
        stdin=stdin,
        pull_request=pull_request,
        github_repository=github_repository,
        template_path=template_path,
        output_format=output_format,
        quiet=quiet,
    )


@github_description_app.command("check")
def check_event_description(
    policy_path: PolicyOption,
    event_file: Annotated[
        Path, typer.Option("--event-file", help="Explicit GitHub pull-request event JSON.")
    ],
    template_path: TemplateOption = None,
    output_format: FormatOption = DescriptionOutputFormat.TEXT,
    quiet: QuietOption = False,
) -> None:
    """Validate the event body without Git history, API access, or author exemptions."""
    _run(
        policy_path=policy_path,
        event_file=event_file,
        template_path=template_path,
        output_format=output_format,
        quiet=quiet,
    )


def _run(
    *,
    policy_path: Path,
    output_format: DescriptionOutputFormat,
    quiet: bool,
    file_path: Path | None = None,
    stdin: bool = False,
    event_file: Path | None = None,
    pull_request: str | None = None,
    github_repository: str | None = None,
    template_path: Path | None = None,
) -> None:
    try:
        checked = check_description_source(
            policy_path=policy_path,
            file_path=file_path,
            stdin=stdin,
            event_file=event_file,
            pull_request=pull_request,
            github_repository=github_repository,
            template_path=template_path,
        )
    except YagaError as error:
        typer.echo(render_description_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    if not quiet:
        typer.echo(render_description_report(checked, output_format))
    if not checked.report.valid:
        raise typer.Exit(code=1)
