"""Stable text, JSON, and GitHub reports for workflow-security policy."""

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
from yaga.workflows.models import WorkflowDiagnostic, WorkflowOutputFormat
from yaga.workflows.reporting import (
    MAX_DIAGNOSTIC_CODE,
    MAX_GITHUB_ANNOTATIONS,
    MAX_GITHUB_TITLE,
)
from yaga.workflows.security_models import (
    WorkflowSecurityReport,
    WorkflowSecurityResult,
)

MAX_PROFILE_NAME = 64
MAX_RULE_NAME = 100


def render_workflow_security_report(
    report: WorkflowSecurityReport,
    output_format: WorkflowOutputFormat,
) -> str:
    """Render one complete workflow-security report."""
    if output_format is WorkflowOutputFormat.JSON:
        return json.dumps(
            workflow_security_report_document(report),
            ensure_ascii=False,
            indent=2,
        )
    if output_format is WorkflowOutputFormat.GITHUB:
        return _render_github_report(report)
    return _render_text_report(report)


def render_workflow_security_error(
    error: YagaError,
    output_format: WorkflowOutputFormat,
) -> str:
    """Render one expected workflow-security operational error."""
    message = safe_error_text(error, maximum=MAX_DIAGNOSTIC_MESSAGE)
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
        title = _github_property("YAGA workflow security", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"YAGA {error.kind} error: {message}",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        return f"::error title={title}::{data}"
    return f"YAGA {error.kind} error: {message}"


def workflow_security_report_document(report: WorkflowSecurityReport) -> dict[str, Any]:
    """Return the bounded JSON-ready workflow-security report."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "github_workflow_security",
        "profile": json_text(report.profile.value, maximum=MAX_PROFILE_NAME),
        "rules": [json_text(rule.value, maximum=MAX_RULE_NAME) for rule in report.rules],
        "valid": report.valid,
        "checked": report.checked,
        "passed": report.passed,
        "failed": report.failed,
        "diagnostics": report.diagnostics,
        "workflows": [_result_document(result) for result in report.results],
    }


def _result_document(result: WorkflowSecurityResult) -> dict[str, Any]:
    return {
        "path": json_text(result.path, maximum=MAX_DISPLAY_PATH),
        "status": result.status,
        "valid": result.valid,
        "diagnostics": [_diagnostic_document(item) for item in result.diagnostics],
    }


def _diagnostic_document(diagnostic: WorkflowDiagnostic) -> dict[str, object]:
    return {
        "code": json_text(diagnostic.code, maximum=MAX_DIAGNOSTIC_CODE),
        "message": json_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE),
        "line": diagnostic.line,
        "column": diagnostic.column,
    }


def _render_text_report(report: WorkflowSecurityReport) -> str:
    lines = [_selection_summary(report)]
    for result in report.results:
        path = safe_text(result.path, maximum=MAX_DISPLAY_PATH)
        lines.append(f"{result.status.upper():7} {path}  {len(result.diagnostics)} diagnostic(s)")
        for diagnostic in result.diagnostics:
            code = safe_text(diagnostic.code, maximum=MAX_DIAGNOSTIC_CODE)
            message = safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
            lines.append(
                f"         [{code}] line {diagnostic.line}, column {diagnostic.column}: {message}"
            )
    lines.append(_summary(report, prefix="Checked"))
    return "\n".join(lines)


def _render_github_report(report: WorkflowSecurityReport) -> str:
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
        title = _github_property("YAGA workflow security", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"{omitted} additional workflow security diagnostic(s) omitted",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        lines.append(f"::error title={title}::{data}")
    lines.append(_summary(report, prefix="YAGA security checked"))
    return "\n".join(lines)


def _github_annotation(
    result: WorkflowSecurityResult,
    diagnostic: WorkflowDiagnostic,
) -> str:
    path = _github_property(result.path, maximum=MAX_DISPLAY_PATH)
    title = _github_property(f"YAGA {diagnostic.code}", maximum=MAX_GITHUB_TITLE)
    data = _github_data(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
    return (
        f"::error file={path},line={diagnostic.line},col={diagnostic.column},title={title}::{data}"
    )


def _selection_summary(report: WorkflowSecurityReport) -> str:
    profile = report.profile.value
    rules = ", ".join(rule.value for rule in report.rules) or "none"
    return (
        f"Profile: {safe_text(profile, maximum=MAX_PROFILE_NAME)}; "
        f"rules: {safe_text(rules, maximum=MAX_RULE_NAME * max(len(report.rules), 1))}."
    )


def _summary(report: WorkflowSecurityReport, *, prefix: str) -> str:
    return (
        f"{prefix} {report.checked} workflow file(s): {report.passed} passed, "
        f"{report.failed} failed; {report.diagnostics} diagnostic(s)."
    )


def _github_property(value: str, *, maximum: int) -> str:
    escaped = _github_escape(value).replace(":", "%3A").replace(",", "%2C")
    return _bounded_github_escape(escaped, maximum=maximum)


def _github_data(value: str, *, maximum: int) -> str:
    return _bounded_github_escape(_github_escape(value), maximum=maximum)


def _github_escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _bounded_github_escape(value: str, *, maximum: int) -> str:
    """Sanitize escaped output and truncate without splitting a percent escape."""
    cleaned = json_text(value, maximum=len(value))
    if len(cleaned) <= maximum:
        return cleaned
    cutoff = maximum - 1
    last_escape = cleaned.rfind("%", 0, cutoff)
    if last_escape >= 0 and cutoff - last_escape < 3:
        cutoff = last_escape
    return f"{cleaned[:cutoff]}…"
