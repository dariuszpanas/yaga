"""Installation maintenance commands, independent of repository policy."""

from __future__ import annotations

from typing import Annotated

import typer

from yaga.errors import YagaError, safe_error_text
from yaga.self_update import self_update

app = typer.Typer(help="Maintain the current YAGA installation.", no_args_is_help=True)


@app.command("update")
def update(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Verify ownership and show the update command.")
    ] = False,
) -> None:
    """Update a uv tool installation using its existing constraints and extras."""
    try:
        result = self_update(dry_run=dry_run)
    except YagaError as error:
        typer.echo(safe_error_text(error), err=True)
        raise typer.Exit(2) from error
    if dry_run:
        typer.echo("Update command: " + safe_error_text(subprocess_command(result.command)))
    elif result.log is not None:
        typer.echo(
            "Update scheduled after YAGA exits. Completion log: " + safe_error_text(result.log)
        )
        typer.echo("Wait for YAGA_UPDATE_EXIT_CODE=0 in that log, then run yaga --version.")
    else:
        typer.echo("uv completed the update. Run yaga --version to inspect the installed version.")


def subprocess_command(command: tuple[str, ...]) -> str:
    """Display arguments unambiguously without producing an executable shell snippet."""
    return repr(list(command))
