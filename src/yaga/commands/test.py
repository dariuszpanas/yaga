"""Installed commands for explicit test qualification planning and evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from yaga.errors import InputError, YagaError, safe_error_text
from yaga.test_planning.diagnostics import parse_plan_document
from yaga.test_planning.evaluation import (
    EvaluationReport,
    encode_evaluation,
    evaluate_results,
)
from yaga.test_planning.evaluation_service import load_evaluation_json
from yaga.test_planning.policy import load_test_policy
from yaga.test_planning.reporting import (
    TestOutputFormat,
    render_test_error,
    render_test_plan,
    render_test_policy,
)
from yaga.test_planning.results import MAX_ATTEMPT, parse_results_document
from yaga.test_planning.service import create_test_plan

app = typer.Typer(
    help="Plan test qualification and evaluate receipts without executing tests.",
    no_args_is_help=True,
)
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


@app.command("evaluate")
def evaluate(
    plan_path: Annotated[Path, typer.Option("--plan", help="Explicit version-one plan JSON.")],
    results_path: Annotated[
        Path, typer.Option("--results", help="Explicit version-one result receipts JSON.")
    ],
    policy_sha256: Annotated[
        str, typer.Option("--policy-sha256", help="Independent expected policy SHA-256.")
    ],
    bindings_sha256: Annotated[
        str, typer.Option("--bindings-sha256", help="Independent expected registry SHA-256.")
    ],
    execution_commit: Annotated[
        str, typer.Option("--execution-commit", help="Independent expected exact commit object ID.")
    ],
    execution_tree: Annotated[
        str, typer.Option("--execution-tree", help="Independent expected exact tree object ID.")
    ],
    run_id: Annotated[str, typer.Option("--run-id", help="Independent expected run identity.")],
    attempt: Annotated[
        str, typer.Option("--attempt", help="Independent expected positive attempt.")
    ],
    output_format: Annotated[
        TestOutputFormat,
        typer.Option("--format", case_sensitive=False, help="Report format."),
    ] = TestOutputFormat.TEXT,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress passed output; retain failures and errors."),
    ] = False,
) -> None:
    """Evaluate explicit receipts against independent identities without Git or execution."""
    try:
        expected_attempt = _parse_attempt(attempt)
        planned = parse_plan_document(load_evaluation_json(plan_path))
        results = parse_results_document(load_evaluation_json(results_path))
        report = evaluate_results(
            planned,
            results,
            expected_policy_sha256=policy_sha256,
            expected_bindings_sha256=bindings_sha256,
            expected_execution_commit=execution_commit,
            expected_execution_tree=execution_tree,
            expected_run_id=run_id,
            expected_attempt=expected_attempt,
        )
        rendered = _render_evaluation(report, output_format)
    except YagaError as error:
        typer.echo(_render_evaluation_error(error, output_format), err=True)
        raise typer.Exit(code=2) from error
    if not quiet or not report.passed:
        typer.echo(rendered, nl=output_format is not TestOutputFormat.JSON)
    raise typer.Exit(code=report.exit_code)


def _parse_attempt(value: str) -> int:
    """Keep numeric conversion and malformed option text inside bounded reporting."""
    message = "test attempt must be an integer from 1 through 2147483647"
    if len(value) > 10 or not value.isascii() or not value.isdecimal():
        raise InputError(message)
    attempt = int(value)
    if not 1 <= attempt <= MAX_ATTEMPT:
        raise InputError(message)
    return attempt


def _render_evaluation(report: EvaluationReport, output_format: TestOutputFormat) -> str:
    encoded = encode_evaluation(report)
    if output_format is TestOutputFormat.JSON:
        return encoded.decode("utf-8")
    lines = [
        f"Test evaluation: {report.status}",
        f"Complete: {'yes' if report.complete else 'no'}",
        f"Execution commit: {report.source.execution_commit}",
        f"Execution tree: {report.source.execution_tree}",
        f"Run: {report.run_id}; attempt: {report.attempt}",
        f"Required cells: {report.counts.selected_cells}; "
        f"received: {report.counts.received_cells}; passed: {report.counts.passed}",
        f"Diagnostics: {report.diagnostic_count}",
    ]
    for diagnostic in report.diagnostics[:256]:
        target = f"{diagnostic.suite}/{diagnostic.variant}"
        lines.append(
            f"  [{safe_error_text(diagnostic.code, maximum=64)}] "
            f"{safe_error_text(target, maximum=160)}: "
            f"{safe_error_text(diagnostic.message, maximum=300)}"
        )
    omitted = report.diagnostic_count - min(len(report.diagnostics), 256)
    if omitted:
        lines.append(f"  {omitted} additional diagnostic(s) omitted from this display")
    lines.append("Evaluation of supplied receipts only; no tests were executed.")
    return "\n".join(lines)


def _render_evaluation_error(error: YagaError, output_format: TestOutputFormat) -> str:
    kind = safe_error_text(error.kind, maximum=64)
    message = safe_error_text(error, maximum=1_000)
    if output_format is TestOutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": 1,
                "kind": "test_evaluation",
                "status": "error",
                "valid": False,
                "error": {"kind": kind, "message": message},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return f"YAGA {kind} error: {message}"
