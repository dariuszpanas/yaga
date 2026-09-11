"""Installed commands for the preserved GitHub status gate."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yaga.action import run_gate
from yaga.agent_review.plan import build_plan, render_plan
from yaga.agent_review.policy import load_policy
from yaga.agent_review.results import evaluate_results, load_results, render_evaluation
from yaga.errors import ConfigurationError, GateError, safe_error_text

app = typer.Typer(help="Run a trusted GitHub gate operation.", no_args_is_help=True)
agent_app = typer.Typer(help="Run the Agent review gate.", no_args_is_help=True)
policy_app = typer.Typer(help="Validate Agent review policy.", no_args_is_help=True)
app.add_typer(agent_app, name="agent-review")
agent_app.add_typer(policy_app, name="policy")


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


@policy_app.command("check")
def policy_check(
    policy_file: Annotated[
        Path,
        typer.Option("--file", help="Explicit TOML policy file to validate."),
    ],
) -> None:
    """Validate named review lenses without contacting a provider."""
    try:
        policy = load_policy(policy_file)
    except ConfigurationError as error:
        typer.echo(f"YAGA failed: {safe_error_text(error)}", err=True)
        raise typer.Exit(code=2) from error
    required = ", ".join(policy.required)
    typer.echo(
        f"Agent review policy passed: {len(policy.lenses)} lens(es), "
        f"{len(policy.required)} required ({required}), aggregation {policy.aggregation}."
    )


@policy_app.command("plan")
def policy_plan(
    policy_file: Annotated[
        Path,
        typer.Option("--file", help="Explicit TOML policy file to expand."),
    ],
    output_format: Annotated[
        str,
        typer.Option("--format", help="Plan format: text or json."),
    ] = "text",
) -> None:
    """Expand named review lenses into deterministic adapter work items."""
    try:
        plan = build_plan(load_policy(policy_file))
        typer.echo(render_plan(plan, output_format.lower()))
    except (ConfigurationError, TypeError, ValueError) as error:
        typer.echo(f"YAGA failed: {safe_error_text(error)}", err=True)
        raise typer.Exit(code=2) from error


@policy_app.command("evaluate")
def policy_evaluate(
    policy_file: Annotated[
        Path,
        typer.Option("--file", help="Explicit TOML policy file to apply."),
    ],
    results_file: Annotated[
        Path,
        typer.Option("--results", help="Explicit JSON adapter result file."),
    ],
    output_format: Annotated[
        str,
        typer.Option("--format", help="Result format: text, json, or github."),
    ] = "text",
) -> None:
    """Validate and aggregate named-lens results without contacting a provider."""
    try:
        policy = load_policy(policy_file)
        results = load_results(results_file)
        aggregate = evaluate_results(policy, results)
        typer.echo(render_evaluation(results, aggregate, output_format.lower()))
    except (ConfigurationError, TypeError, ValueError) as error:
        typer.echo(f"YAGA failed: {safe_error_text(error)}", err=True)
        raise typer.Exit(code=2) from error
    if aggregate.state.value != "passed":
        raise typer.Exit(code=1)
