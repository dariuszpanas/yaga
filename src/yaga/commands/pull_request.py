"""Installed commands for explicit pull-request description and review checks."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.agent_review.evaluation import LensOutcome
from yaga.agent_review.service import (
    ReviewOutputFormat,
    ReviewSubmitMode,
    check_pull_request_review_source,
    render_pull_request_review_error,
    render_pull_request_review_report,
)
from yaga.descriptions.reporting import (
    DescriptionOutputFormat,
    render_description_error,
    render_description_report,
)
from yaga.descriptions.service import check_description_source
from yaga.errors import YagaError
from yaga.pull_request_triage.service import check_triage, render_triage, render_triage_error

app = typer.Typer(help="Check pull-request content.", no_args_is_help=True)
description_app = typer.Typer(help="Check a Markdown PR description.", no_args_is_help=True)
review_app = typer.Typer(help="Check pull-request review state.", no_args_is_help=True)
triage_app = typer.Typer(
    help="Check PR provenance and exact-head maintainer admission.", no_args_is_help=True
)
github_description_app = typer.Typer(
    help="Check the description in one explicit GitHub event.", no_args_is_help=True
)
app.add_typer(description_app, name="description")
app.add_typer(review_app, name="review")
app.add_typer(triage_app, name="triage")

PolicyOption = Annotated[Path, typer.Option("--policy", help="Explicit description policy TOML.")]
ReviewPolicyOption = Annotated[
    Path, typer.Option("--policy", help="Explicit Agent review policy TOML.")
]
TemplateOption = Annotated[
    Path | None,
    typer.Option("--template", help="Explicit Markdown template with required headings."),
]
FormatOption = Annotated[
    DescriptionOutputFormat,
    typer.Option("--format", case_sensitive=False, help="Report format."),
]
ReviewFormatOption = Annotated[
    ReviewOutputFormat,
    typer.Option("--format", case_sensitive=False, help="Report format."),
]
ReviewSubmitOption = Annotated[
    ReviewSubmitMode,
    typer.Option(
        "--submit-review",
        case_sensitive=False,
        help="Submit a commit-bound PR review for --pr: none, approve, request-changes, or both.",
    ),
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


@review_app.command("check")
def check_pull_request_review(
    policy_path: ReviewPolicyOption,
    file_path: Annotated[
        Path | None,
        typer.Option("--file", help="UTF-8 review snapshot JSON; exclusive with --pr."),
    ] = None,
    pull_request: Annotated[
        str | None,
        typer.Option(
            "--pr", help="Fetch the current review state with gh: PR URL or positive number."
        ),
    ] = None,
    github_repository: Annotated[
        str | None,
        typer.Option("--repo", help="[HOST/]OWNER/REPO; required only with a numeric --pr."),
    ] = None,
    wait_seconds: Annotated[
        int | None,
        typer.Option(
            "--wait-seconds",
            help="Bounded poll budget (0..600) while a live --pr review is actively running.",
        ),
    ] = None,
    submit_review: ReviewSubmitOption = ReviewSubmitMode.NONE,
    triage_policy: Annotated[
        Path | None,
        typer.Option(
            "--triage-policy", help="Trusted admission policy; required before APPROVE submission."
        ),
    ] = None,
    output_format: ReviewFormatOption = ReviewOutputFormat.TEXT,
    quiet: QuietOption = False,
) -> None:
    """Validate pull-request review state from a snapshot file or live gh PR against policy."""
    try:
        checked = check_pull_request_review_source(
            policy_path=policy_path,
            file_path=file_path,
            pull_request=pull_request,
            github_repository=github_repository,
            wait_seconds=wait_seconds,
            submit_review=submit_review,
            triage_policy_path=triage_policy,
        )
    except YagaError as error:
        typer.echo(render_pull_request_review_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    if not quiet:
        typer.echo(render_pull_request_review_report(checked, output_format))
    if checked.evaluation.aggregate.state is not LensOutcome.PASSED:
        raise typer.Exit(code=1)
    if checked.admission is not None and checked.admission["admission"]["decision"] != "allow":
        raise typer.Exit(code=1)


@triage_app.command("check")
def check_pull_request_triage(
    policy_path: Annotated[
        Path, typer.Option("--policy", help="Explicit trusted triage policy TOML.")
    ],
    file_path: Annotated[
        Path | None, typer.Option("--file", help="Explicit offline metadata snapshot.")
    ] = None,
    pull_request: Annotated[
        str | None, typer.Option("--pr", help="Explicit PR URL or positive number.")
    ] = None,
    github_repository: Annotated[
        str | None, typer.Option("--repo", help="[HOST/]OWNER/REPO for numeric PR.")
    ] = None,
    publish_comment: Annotated[
        bool,
        typer.Option(
            "--publish-comment", help="Explicitly publish/update the owned bounded triage comment."
        ),
    ] = False,
    output_format: ReviewFormatOption = ReviewOutputFormat.TEXT,
) -> None:
    """Evaluate bounded provenance and review facts; never execute PR content."""
    try:
        report = check_triage(
            policy_path=policy_path,
            file_path=file_path,
            pull_request=pull_request,
            github_repository=github_repository,
            publish=publish_comment,
        )
        typer.echo(render_triage(report, output_format.value))
    except YagaError as error:
        typer.echo(render_triage_error(error, output_format.value), err=True)
        raise typer.Exit(code=2) from error
    if report["admission"]["decision"] != "allow":
        raise typer.Exit(code=1)


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
