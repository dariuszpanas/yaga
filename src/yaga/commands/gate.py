"""Installed commands for the preserved GitHub status gate."""

from __future__ import annotations

import typer

from yaga.action import run_gate
from yaga.errors import GateError, safe_error_text

app = typer.Typer(help="Run a trusted GitHub gate operation.", no_args_is_help=True)
agent_app = typer.Typer(help="Run the Agent review gate.", no_args_is_help=True)
app.add_typer(agent_app, name="agent-review")


def _run(operation: str) -> None:
    try:
        exit_code = run_gate("agent-review", operation)
    except GateError as error:
        typer.echo(f"YAGA failed: {safe_error_text(error)}", err=True)
        raise typer.Exit(code=1) from error
    if exit_code:
        raise typer.Exit(code=exit_code)


@agent_app.command()
def invalidate() -> None:
    """Invalidate inherited status at a trusted lifecycle boundary."""
    _run("invalidate")


@agent_app.command()
def prepare() -> None:
    """Resolve the current CI/lifecycle pair and choose a closed route."""
    _run("prepare")


@agent_app.command()
def authorize() -> None:
    """Record the protected external-request authorization marker."""
    _run("authorize")


@agent_app.command()
def observe() -> None:
    """Observe an already requested Agent review without commenting."""
    _run("observe")


@agent_app.command()
def request() -> None:
    """Request and observe a permitted Agent review."""
    _run("request")


@agent_app.command()
def finalize() -> None:
    """Revalidate evidence and publish terminal statuses."""
    _run("finalize")
