"""Tests for actionlint text, JSON, and GitHub reports."""

from __future__ import annotations

import json

import pytest

from yaga.errors import InputError
from yaga.workflows.models import (
    WorkflowDiagnostic,
    WorkflowLintReport,
    WorkflowLintResult,
    WorkflowOutputFormat,
)
from yaga.workflows.reporting import (
    MAX_DIAGNOSTIC_CODE,
    MAX_GITHUB_ANNOTATIONS,
    render_workflow_lint_error,
    render_workflow_lint_report,
)


def diagnostic(
    code: str = "actionlint.syntax-check",
    message: str = "workflow key is invalid",
    *,
    line: int = 3,
    column: int = 9,
) -> WorkflowDiagnostic:
    return WorkflowDiagnostic(code=code, message=message, line=line, column=column)


def test_lint_models_expose_deterministic_status_and_aggregates() -> None:
    passed = WorkflowLintResult(path="pass.yml", diagnostics=())
    failed = WorkflowLintResult(
        path="fail.yml",
        diagnostics=(diagnostic(), diagnostic("actionlint.expression")),
    )
    report = WorkflowLintReport(results=(passed, failed))

    assert passed.valid is True
    assert passed.status == "passed"
    assert failed.valid is False
    assert failed.status == "failed"
    assert report.checked == 2
    assert report.passed == 1
    assert report.failed == 1
    assert report.diagnostics == 2
    assert report.valid is False


def test_text_lint_report_is_deterministic_sanitized_and_bounded() -> None:
    long_message = "unsafe\x1b\n" + "m" * 600
    report = WorkflowLintReport(
        results=(
            WorkflowLintResult(path=".github/workflows/pass.yml", diagnostics=()),
            WorkflowLintResult(
                path=".github/workflows/fail\n.yml",
                diagnostics=(diagnostic("actionlint.syntax\x00", long_message, line=8, column=4),),
            ),
        )
    )

    rendered = render_workflow_lint_report(report, WorkflowOutputFormat.TEXT)
    lines = rendered.splitlines()

    assert lines[0] == "PASSED  .github/workflows/pass.yml  0 diagnostic(s)"
    assert lines[1] == "FAILED  .github/workflows/fail?.yml  1 diagnostic(s)"
    assert lines[2].startswith("         [actionlint.syntax?] line 8, column 4: unsafe??")
    assert lines[2].endswith("…")
    assert len(lines[2].split(": ", 1)[1]) == 500
    assert lines[3] == "Linted 2 workflow file(s): 1 passed, 1 failed; 1 diagnostic(s)."


def test_json_lint_report_has_the_exact_contract_without_actionlint_snippets() -> None:
    report = WorkflowLintReport(
        results=(
            WorkflowLintResult(
                path=".github/workflows/ci\n.yml",
                diagnostics=(diagnostic(message="bad\x00workflow", line=7, column=11),),
            ),
            WorkflowLintResult(path=".github/workflows/release.yml", diagnostics=()),
        )
    )

    document = json.loads(render_workflow_lint_report(report, WorkflowOutputFormat.JSON))

    assert document == {
        "schema_version": 1,
        "kind": "github_workflow_lint",
        "valid": False,
        "checked": 2,
        "passed": 1,
        "failed": 1,
        "diagnostics": 1,
        "workflows": [
            {
                "path": ".github/workflows/ci?.yml",
                "status": "failed",
                "valid": False,
                "diagnostics": [
                    {
                        "code": "actionlint.syntax-check",
                        "message": "bad?workflow",
                        "line": 7,
                        "column": 11,
                    }
                ],
            },
            {
                "path": ".github/workflows/release.yml",
                "status": "passed",
                "valid": True,
                "diagnostics": [],
            },
        ],
    }
    serialized = json.dumps(document)
    assert "snippet" not in serialized
    assert "end_column" not in serialized
    assert "references_checked" not in serialized


def test_github_lint_annotations_escape_properties_data_and_controls() -> None:
    report = WorkflowLintReport(
        results=(
            WorkflowLintResult(
                path=".github/workflows/a%,:\r\n.yml",
                diagnostics=(
                    diagnostic(
                        "actionlint.%,:\r\n",
                        "bad%\r\nmessage\x00",
                        line=7,
                        column=5,
                    ),
                ),
            ),
        )
    )

    annotation, summary = render_workflow_lint_report(
        report, WorkflowOutputFormat.GITHUB
    ).splitlines()

    assert annotation == (
        "::error file=.github/workflows/a%25%2C%3A%0D%0A.yml,line=7,col=5,"
        "title=YAGA actionlint.%25%2C%3A%0D%0A::bad%25%0D%0Amessage?"
    )
    assert summary == "YAGA linted 1 workflow file(s): 0 passed, 1 failed; 1 diagnostic(s)."


def test_github_lint_report_caps_annotations_and_reports_omission() -> None:
    diagnostics = tuple(
        diagnostic(code=f"actionlint.policy.{index}", message=f"failure {index}")
        for index in range(60)
    )
    report = WorkflowLintReport(
        results=(WorkflowLintResult(path=".github/workflows/ci.yml", diagnostics=diagnostics),)
    )

    lines = render_workflow_lint_report(report, WorkflowOutputFormat.GITHUB).splitlines()
    annotations = [line for line in lines if line.startswith("::error")]

    assert len(annotations) == MAX_GITHUB_ANNOTATIONS
    assert "title=YAGA actionlint.policy.48::failure 48" in annotations[-2]
    assert annotations[-1] == (
        "::error title=YAGA workflow lint::11 additional workflow lint diagnostic(s) omitted"
    )
    assert lines[-1] == ("YAGA linted 1 workflow file(s): 0 passed, 1 failed; 60 diagnostic(s).")


@pytest.mark.parametrize(
    ("output_format", "expected"),
    [
        (
            WorkflowOutputFormat.TEXT,
            "Linted 0 workflow file(s): 0 passed, 0 failed; 0 diagnostic(s).",
        ),
        (
            WorkflowOutputFormat.GITHUB,
            "YAGA linted 0 workflow file(s): 0 passed, 0 failed; 0 diagnostic(s).",
        ),
    ],
)
def test_empty_text_and_github_lint_reports_are_aggregate_only(
    output_format: WorkflowOutputFormat,
    expected: str,
) -> None:
    assert render_workflow_lint_report(WorkflowLintReport(results=()), output_format) == expected


def test_empty_json_lint_report_has_zero_aggregates() -> None:
    document = json.loads(
        render_workflow_lint_report(WorkflowLintReport(results=()), WorkflowOutputFormat.JSON)
    )

    assert document == {
        "schema_version": 1,
        "kind": "github_workflow_lint",
        "valid": True,
        "checked": 0,
        "passed": 0,
        "failed": 0,
        "diagnostics": 0,
        "workflows": [],
    }


def test_lint_diagnostic_code_is_bounded_in_text_and_json() -> None:
    code = "c" * (MAX_DIAGNOSTIC_CODE + 20)
    report = WorkflowLintReport(
        results=(WorkflowLintResult(path="workflow.yml", diagnostics=(diagnostic(code=code),)),)
    )

    text = render_workflow_lint_report(report, WorkflowOutputFormat.TEXT)
    document = json.loads(render_workflow_lint_report(report, WorkflowOutputFormat.JSON))

    assert f"[{'c' * 99}…]" in text
    assert document["workflows"][0]["diagnostics"][0]["code"] == f"{'c' * 99}…"


def test_lint_operational_github_error_uses_the_lint_title() -> None:
    rendered = render_workflow_lint_error(
        InputError("Docker is unavailable"),
        WorkflowOutputFormat.GITHUB,
    )

    assert rendered == "::error title=YAGA workflow lint::YAGA input error: Docker is unavailable"
