"""Stable text, JSON, and GitHub reports for workflow policy checks."""

from __future__ import annotations

import json
from typing import Any

from yaga.commits.reporting import (
    MAX_DIAGNOSTIC_MESSAGE,
    MAX_DISPLAY_PATH,
    SCHEMA_VERSION,
    json_text,
    safe_text,
)
from yaga.errors import YagaError, safe_error_text
from yaga.workflows.models import (
    WorkflowDiagnostic,
    WorkflowOutputFormat,
    WorkflowReport,
    WorkflowResult,
)

MAX_DIAGNOSTIC_CODE = 100
MAX_GITHUB_ANNOTATIONS = 50
MAX_GITHUB_TITLE = 120


def render_workflow_report(
    report: WorkflowReport,
    output_format: WorkflowOutputFormat,
) -> str:
    """Render one complete workflow-policy report."""
    if output_format is WorkflowOutputFormat.JSON:
        return json.dumps(_report_document(report), ensure_ascii=False, indent=2)
    if output_format is WorkflowOutputFormat.GITHUB:
        return _render_github_report(report)
    return _render_text_report(report)


def render_workflow_error(
    error: YagaError,
    output_format: WorkflowOutputFormat,
) -> str:
    """Render one expected workflow-policy operational error."""
    message = safe_error_text(error)
    if output_format is WorkflowOutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        )
    if output_format is WorkflowOutputFormat.GITHUB:
        title = _workflow_property("YAGA workflow policy", maximum=MAX_GITHUB_TITLE)
        data = _workflow_data(
            f"YAGA {error.kind} error: {message}",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        return f"::error title={title}::{data}"
    return f"YAGA {error.kind} error: {message}"


def _render_text_report(report: WorkflowReport) -> str:
    lines: list[str] = []
    for result in report.results:
        path = safe_text(result.path, maximum=MAX_DISPLAY_PATH)
        lines.append(f"{result.status.upper():7} {path}  {result.references_checked} reference(s)")
        for diagnostic in result.diagnostics:
            code = safe_text(diagnostic.code, maximum=MAX_DIAGNOSTIC_CODE)
            message = safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
            lines.append(
                f"         [{code}] line {diagnostic.line}, column {diagnostic.column}: {message}"
            )
    lines.append(_summary(report, prefix="Checked"))
    return "\n".join(lines)


def _report_document(report: WorkflowReport) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "github_workflow_policy",
        "valid": report.valid,
        "checked": report.checked,
        "passed": report.passed,
        "failed": report.failed,
        "references_checked": report.references_checked,
        "workflows": [_result_document(result) for result in report.results],
    }


def _result_document(result: WorkflowResult) -> dict[str, Any]:
    return {
        "path": json_text(result.path, maximum=MAX_DISPLAY_PATH),
        "status": result.status,
        "valid": result.valid,
        "references_checked": result.references_checked,
        "diagnostics": [_diagnostic_document(item) for item in result.diagnostics],
    }


def _diagnostic_document(diagnostic: WorkflowDiagnostic) -> dict[str, object]:
    return {
        "code": json_text(diagnostic.code, maximum=MAX_DIAGNOSTIC_CODE),
        "message": json_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE),
        "line": diagnostic.line,
        "column": diagnostic.column,
    }


def _render_github_report(report: WorkflowReport) -> str:
    diagnostics = [
        (result, diagnostic) for result in report.results for diagnostic in result.diagnostics
    ]
    visible = diagnostics
    omitted = 0
    if len(diagnostics) > MAX_GITHUB_ANNOTATIONS:
        visible = diagnostics[: MAX_GITHUB_ANNOTATIONS - 1]
        omitted = len(diagnostics) - len(visible)

    lines = [_github_annotation(result, diagnostic) for result, diagnostic in visible]
    if omitted:
        title = _workflow_property("YAGA workflow policy", maximum=MAX_GITHUB_TITLE)
        data = _workflow_data(
            f"{omitted} additional workflow diagnostic(s) omitted",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        lines.append(f"::error title={title}::{data}")
    lines.append(_summary(report, prefix="YAGA checked"))
    return "\n".join(lines)


def _github_annotation(
    result: WorkflowResult,
    diagnostic: WorkflowDiagnostic,
) -> str:
    path = _workflow_property(result.path, maximum=MAX_DISPLAY_PATH)
    title = _workflow_property(
        f"YAGA {diagnostic.code}",
        maximum=MAX_GITHUB_TITLE,
    )
    message = _workflow_data(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
    return (
        f"::error file={path},line={diagnostic.line},col={diagnostic.column},"
        f"title={title}::{message}"
    )


def _summary(report: WorkflowReport, *, prefix: str) -> str:
    return (
        f"{prefix} {report.checked} workflow file(s): {report.passed} passed, "
        f"{report.failed} failed; {report.references_checked} reference(s)."
    )


def _workflow_property(value: str, *, maximum: int) -> str:
    """Escape and bound one GitHub workflow-command property."""
    escaped = _workflow_escape(value).replace(":", "%3A").replace(",", "%2C")
    return _bounded_workflow_escape(escaped, maximum=maximum)


def _workflow_data(value: str, *, maximum: int) -> str:
    """Escape and bound GitHub workflow-command data."""
    return _bounded_workflow_escape(_workflow_escape(value), maximum=maximum)


def _workflow_escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _bounded_workflow_escape(value: str, *, maximum: int) -> str:
    """Sanitize escaped output and truncate without splitting a percent escape."""
    cleaned = json_text(value, maximum=len(value))
    if len(cleaned) <= maximum:
        return cleaned
    cutoff = maximum - 1
    last_escape = cleaned.rfind("%", 0, cutoff)
    if last_escape >= 0 and cutoff - last_escape < 3:
        cutoff = last_escape
    return f"{cleaned[:cutoff]}…"
