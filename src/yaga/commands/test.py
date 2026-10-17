"""Installed commands for explicit test qualification planning."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.errors import YagaError
from yaga.test_planning.policy import load_test_policy
from yaga.test_planning.reporting import (
    TestOutputFormat,
    render_test_error,
    render_test_plan,
    render_test_policy,
)
from yaga.test_planning.service import create_test_plan

app = typer.Typer(help="Plan test qualification without executing tests.", no_args_is_help=True)
policy_app = typer.Typer(
    help="Validate an explicit test qualification policy.", no_args_is_help=True
)
app.add_typer(policy_app, name="policy")


@policy_app.command("check")
def check_policy(
    policy_path: Annotated[
        Path, typer.Option("--file", help="Explicit versioned test policy file.")
    ],
    output_format: Annotated[
        TestOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = TestOutputFormat.TEXT,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress valid policy output; retain errors.")
    ] = False,
) -> None:
    """Validate one policy without Git access, discovery, or execution."""
    try:
        loaded = load_test_policy(policy_path)
        rendered = render_test_policy(loaded, output_format)
    except YagaError as error:
        typer.echo(render_test_error(error, output_format, kind="test_policy"), err=True)
        raise typer.Exit(code=2) from error
    if not quiet:
        typer.echo(rendered, nl=output_format is not TestOutputFormat.JSON)


@app.command("plan")
def plan(
    policy_path: Annotated[
        Path, typer.Option("--policy", help="Explicit versioned test policy file.")
    ],
    profile: Annotated[str, typer.Option("--profile", help="Exact policy profile name.")],
    revision_range: Annotated[
        str | None, typer.Option("--range", help="Exact A..B or A...B Git range.")
    ] = None,
    revision: Annotated[
        str | None, typer.Option("--revision", help="Exact commit-ish; full profiles only.")
    ] = None,
    repository: Annotated[
        Path, typer.Option("--repo", help="Git repository containing the selected objects.")
    ] = Path("."),
    bindings_sha256: Annotated[
        str | None,
        typer.Option(
            "--bindings-sha256", help="Lowercase SHA-256 of the fixed execution manifest."
        ),
    ] = None,
    output_format: Annotated[
        TestOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = TestOutputFormat.TEXT,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress valid plan output; retain errors.")
    ] = False,
) -> None:
    """Select suites for one explicit source without running tests or fetching history."""
    try:
        planned = create_test_plan(
            repository,
            policy_path=policy_path,
            profile=profile,
            revision_range=revision_range,
            revision=revision,
            bindings_sha256=bindings_sha256,
        )
        rendered = render_test_plan(planned, output_format)
    except YagaError as error:
        typer.echo(render_test_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    if not quiet:
        typer.echo(rendered, nl=output_format is not TestOutputFormat.JSON)
