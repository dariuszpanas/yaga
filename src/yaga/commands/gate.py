"""Installed commands for the preserved GitHub status gate."""

from __future__ import annotations

import typer

from yaga.action import run_gate
from yaga.errors import GateError, safe_error_text

app = typer.Typer(help="Run a trusted GitHub gate operation.", no_args_is_help=True)
codex_app = typer.Typer(help="Run the Codex review gate.", no_args_is_help=True)
app.add_typer(codex_app, name="codex-review")


def _run(operation: str) -> None:
    try:
        exit_code = run_gate("codex-review", operation)
    except GateError as error:
        typer.echo(f"YAGA failed: {safe_error_text(error)}", err=True)
        raise typer.Exit(code=1) from error
    if exit_code:
        raise typer.Exit(code=exit_code)


@codex_app.command()
def invalidate() -> None:
    """Invalidate inherited status at a trusted lifecycle boundary."""
    _run("invalidate")


@codex_app.command()
def prepare() -> None:
    """Resolve the current CI/lifecycle pair and choose a closed route."""
    _run("prepare")


@codex_app.command()
def authorize() -> None:
    """Record the protected external-request authorization marker."""
    _run("authorize")


@codex_app.command()
def observe() -> None:
    """Observe an already requested Codex review without commenting."""
    _run("observe")


@codex_app.command()
def request() -> None:
    """Request and observe a permitted Codex review."""
    _run("request")


@codex_app.command()
def finalize() -> None:
    """Revalidate evidence and publish terminal statuses."""
    _run("finalize")
