"""Installed CLI commands for initializing and auditing YAGA policy files."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.errors import InputError, YagaError
from yaga.policies.audit import audit_policies
from yaga.policies.init_templates import existing_policy_status, initialize_policies
from yaga.policies.reporting import (
    PolicyInitOutputFormat,
    PolicyOutputFormat,
    render_policy_audit,
    render_policy_error,
    render_policy_init,
)
from yaga.policies.schemas import POLICY_SPECS, PolicyKind
from yaga.policies.selector import is_interactive_terminal, select_policies_interactively

app = typer.Typer(
    help="Initialize starter policies and audit schema versions across TOML files.",
    no_args_is_help=True,
)


@app.command("init")
def init_policy(
    policies: Annotated[
        list[PolicyKind] | None,
        typer.Argument(
            help=(
                "Policy families to initialize "
                "(omit in an interactive terminal to open the checkbox selector)."
            ),
        ),
    ] = None,
    select_all: Annotated[
        bool,
        typer.Option("--all", help="Initialize every supported YAGA policy family."),
    ] = False,
    skip_existing: Annotated[
        bool,
        typer.Option(
            "--skip-existing",
            help="Skip policy files that already exist instead of failing.",
        ),
    ] = False,
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Repository directory that will receive policy files."),
    ] = Path("."),
    output_format: Annotated[
        PolicyInitOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = PolicyInitOutputFormat.TEXT,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Validate and preview starter policies without creating files.",
        ),
    ] = False,
) -> None:
    """Create starter YAGA policy files stamped with the latest schema versions."""
    try:
        if policies and select_all:
            raise InputError("choose either explicit policy names or --all")
        if select_all:
            selected_kinds: tuple[PolicyKind, ...] = tuple(POLICY_SPECS.keys())
        elif policies:
            selected_kinds = tuple(policies)
        elif output_format is PolicyInitOutputFormat.TEXT and is_interactive_terminal():
            existing = existing_policy_status(repository)
            chosen = select_policies_interactively(existing)
            if chosen is None:
                typer.echo("No policies initialized (cancelled).")
                raise typer.Exit(code=0)
            if not chosen:
                typer.echo("No policies selected; no files created.")
                raise typer.Exit(code=0)
            selected_kinds = chosen
        else:
            raise InputError(
                "select one or more policy names, pass --all, or run interactively in a terminal"
            )

        report = initialize_policies(
            repository,
            selected_kinds,
            dry_run=dry_run,
            skip_existing=skip_existing,
        )
    except YagaError as error:
        typer.echo(render_policy_error(error, output_format), err=True)
        raise typer.Exit(code=2) from None

    typer.echo(render_policy_init(report, output_format))


@app.command("check")
def check_policies(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Explicit policy TOML files to check; omit to audit policies in --repo.",
        ),
    ] = None,
    repository: Annotated[
        Path,
        typer.Option("--repo", help="Repository directory to scan when no paths are passed."),
    ] = Path("."),
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit 1 when any valid policy uses an older deprecated schema version.",
        ),
    ] = False,
    output_format: Annotated[
        PolicyOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = PolicyOutputFormat.TEXT,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress non-error output; retain exit codes."),
    ] = False,
) -> None:
    """Validate policy TOML files, required-version floors, and latest schema versions."""
    try:
        report = audit_policies(
            repository=repository,
            paths=paths or (),
            strict=strict,
        )
    except YagaError as error:
        typer.echo(render_policy_error(error, output_format), err=True)
        raise typer.Exit(code=2) from None

    rendered = render_policy_audit(report, output_format)
    if report.exit_code == 2:
        typer.echo(rendered, err=True)
        raise typer.Exit(code=2)
    if not quiet:
        typer.echo(rendered)
    if report.exit_code != 0:
        raise typer.Exit(code=report.exit_code)
