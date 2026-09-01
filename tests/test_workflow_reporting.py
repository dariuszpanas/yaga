"""Tests for workflow-policy text, JSON, and GitHub reports."""

from __future__ import annotations

import json

import pytest

from yaga.errors import InputError
from yaga.workflows.models import (
    WorkflowDiagnostic,
    WorkflowOutputFormat,
    WorkflowReport,
    WorkflowResult,
)
from yaga.workflows.reporting import (
    MAX_DIAGNOSTIC_CODE,
    MAX_GITHUB_ANNOTATIONS,
    render_workflow_error,
    render_workflow_report,
)


def diagnostic(
    code: str = "action.pin",
    message: str = "action references must use a full commit SHA",
    *,
    line: int = 3,
    column: int = 9,
) -> WorkflowDiagnostic:
    return WorkflowDiagnostic(code=code, message=message, line=line, column=column)


def test_text_report_is_deterministic_sanitized_and_bounded() -> None:
    long_path = ".github/workflows/" + "p" * 1100 + "\u202e.yml"
    long_message = "unsafe\x1b\n" + "m" * 600
    report = WorkflowReport(
        results=(
            WorkflowResult(path=".github/workflows/pass.yml", references_checked=2, diagnostics=()),
            WorkflowResult(
                path=long_path,
                references_checked=1,
                diagnostics=(diagnostic("pin\x00code", long_message, line=8, column=4),),
            ),
        )
    )

    rendered = render_workflow_report(report, WorkflowOutputFormat.TEXT)
    lines = rendered.splitlines()

    assert lines[0] == "PASSED  .github/workflows/pass.yml  2 reference(s)"
    assert lines[1].startswith("FAILED  .github/workflows/")
    assert lines[1].endswith("…  1 reference(s)")
    assert "\u202e" not in rendered
    assert lines[2].startswith("         [pin?code] line 8, column 4: unsafe??")
    assert lines[2].endswith("…")
    assert len(lines[2].split(": ", 1)[1]) == 500
    assert lines[3] == ("Checked 2 workflow file(s): 1 passed, 1 failed; 3 reference(s).")


def test_json_report_has_the_exact_public_contract_without_raw_references() -> None:
    report = WorkflowReport(
        results=(
            WorkflowResult(
                path=".github/workflows/ci\n.yml",
                references_checked=3,
                diagnostics=(diagnostic(message="bad\x00reference", line=7, column=11),),
            ),
            WorkflowResult(
                path=".github/workflows/release.yml", references_checked=1, diagnostics=()
            ),
        )
    )

    document = json.loads(render_workflow_report(report, WorkflowOutputFormat.JSON))

    assert document == {
        "schema_version": 1,
        "kind": "github_workflow_policy",
        "valid": False,
        "checked": 2,
        "passed": 1,
        "failed": 1,
        "references_checked": 4,
        "workflows": [
            {
                "path": ".github/workflows/ci?.yml",
                "status": "failed",
                "valid": False,
                "references_checked": 3,
                "diagnostics": [
                    {
                        "code": "action.pin",
                        "message": "bad?reference",
                        "line": 7,
                        "column": 11,
                    }
                ],
            },
            {
                "path": ".github/workflows/release.yml",
                "status": "passed",
                "valid": True,
                "references_checked": 1,
                "diagnostics": [],
            },
        ],
    }
    assert "uses" not in json.dumps(document)


def test_github_annotations_escape_properties_data_and_controls() -> None:
    report = WorkflowReport(
        results=(
            WorkflowResult(
                path=".github/workflows/a%,:\r\n.yml",
                references_checked=1,
                diagnostics=(
                    diagnostic(
                        "pin%,:\r\ncode",
                        "bad%\r\nmessage\x00",
                        line=7,
                        column=5,
                    ),
                ),
            ),
        )
    )

    rendered = render_workflow_report(report, WorkflowOutputFormat.GITHUB)
    annotation, summary = rendered.splitlines()

    assert annotation == (
        "::error file=.github/workflows/a%25%2C%3A%0D%0A.yml,line=7,col=5,"
        "title=YAGA pin%25%2C%3A%0D%0Acode::bad%25%0D%0Amessage?"
    )
    assert summary == "YAGA checked 1 workflow file(s): 0 passed, 1 failed; 1 reference(s)."


def test_github_annotations_bound_properties_messages_and_titles() -> None:
    report = WorkflowReport(
        results=(
            WorkflowResult(
                path="%," * 600,
                references_checked=0,
                diagnostics=(diagnostic("%:" * 200, "%\r\n" * 200),),
            ),
        )
    )

    annotation = render_workflow_report(report, WorkflowOutputFormat.GITHUB).splitlines()[0]
    properties, message = annotation.removeprefix("::error ").split("::", 1)
    path_property, _line, _column, title_property = properties.split(",")

    path_value = path_property.removeprefix("file=")
    title_value = title_property.removeprefix("title=")
    for value, maximum in (
        (path_value, 1000),
        (title_value, 120),
        (message, 500),
    ):
        assert len(value) <= maximum
        assert value.endswith("…")
        _assert_complete_workflow_escapes(value)


def test_github_report_caps_annotations_and_uses_the_last_for_omission() -> None:
    diagnostics = tuple(
        diagnostic(code=f"policy.{index}", message=f"failure {index}") for index in range(60)
    )
    report = WorkflowReport(
        results=(
            WorkflowResult(
                path=".github/workflows/ci.yml",
                references_checked=60,
                diagnostics=diagnostics,
            ),
        )
    )

    lines = render_workflow_report(report, WorkflowOutputFormat.GITHUB).splitlines()
    annotations = [line for line in lines if line.startswith("::error")]

    assert len(annotations) == MAX_GITHUB_ANNOTATIONS
    assert "title=YAGA policy.48::failure 48" in annotations[-2]
    assert annotations[-1] == (
        "::error title=YAGA workflow policy::11 additional workflow diagnostic(s) omitted"
    )
    assert lines[-1] == ("YAGA checked 1 workflow file(s): 0 passed, 1 failed; 60 reference(s).")


@pytest.mark.parametrize(
    ("output_format", "expected"),
    [
        (
            WorkflowOutputFormat.TEXT,
            "Checked 0 workflow file(s): 0 passed, 0 failed; 0 reference(s).",
        ),
        (
            WorkflowOutputFormat.GITHUB,
            "YAGA checked 0 workflow file(s): 0 passed, 0 failed; 0 reference(s).",
        ),
    ],
)
def test_empty_text_and_github_reports_are_aggregate_only(
    output_format: WorkflowOutputFormat,
    expected: str,
) -> None:
    assert render_workflow_report(WorkflowReport(results=()), output_format) == expected


def test_empty_json_report_has_zero_aggregates() -> None:
    document = json.loads(
        render_workflow_report(WorkflowReport(results=()), WorkflowOutputFormat.JSON)
    )

    assert document["valid"] is True
    assert document["checked"] == 0
    assert document["passed"] == 0
    assert document["failed"] == 0
    assert document["references_checked"] == 0
    assert document["workflows"] == []


def test_passing_github_report_has_only_the_nonzero_summary() -> None:
    report = WorkflowReport(
        results=(
            WorkflowResult(
                path=".github/workflows/ci.yml",
                references_checked=2,
                diagnostics=(),
            ),
        )
    )

    assert render_workflow_report(report, WorkflowOutputFormat.GITHUB) == (
        "YAGA checked 1 workflow file(s): 1 passed, 0 failed; 2 reference(s)."
    )


@pytest.mark.parametrize("output_format", list(WorkflowOutputFormat))
def test_operational_errors_are_sanitized_for_every_format(
    output_format: WorkflowOutputFormat,
) -> None:
    rendered = render_workflow_error(InputError("bad%\r\ninput\x00"), output_format)

    assert "\r" not in rendered
    if output_format is WorkflowOutputFormat.JSON:
        assert json.loads(rendered) == {
            "schema_version": 1,
            "error": {"kind": "input", "message": "bad%??input?"},
        }
    elif output_format is WorkflowOutputFormat.GITHUB:
        assert rendered == ("::error title=YAGA workflow policy::YAGA input error: bad%25??input?")
        assert len(rendered.splitlines()) == 1
    else:
        assert rendered == "YAGA input error: bad%??input?"


def test_diagnostic_code_is_bounded_in_text_and_json() -> None:
    code = "c" * (MAX_DIAGNOSTIC_CODE + 20)
    report = WorkflowReport(
        results=(
            WorkflowResult(
                path="workflow.yml",
                references_checked=0,
                diagnostics=(diagnostic(code=code),),
            ),
        )
    )

    text = render_workflow_report(report, WorkflowOutputFormat.TEXT)
    document = json.loads(render_workflow_report(report, WorkflowOutputFormat.JSON))

    assert f"[{'c' * 99}…]" in text
    assert document["workflows"][0]["diagnostics"][0]["code"] == f"{'c' * 99}…"


def _assert_complete_workflow_escapes(value: str) -> None:
    cursor = 0
    while (escape := value.find("%", cursor)) >= 0:
        encoded = value[escape + 1 : escape + 3]
        assert len(encoded) == 2
        assert all(character in "0123456789ABCDEF" for character in encoded)
        cursor = escape + 3
