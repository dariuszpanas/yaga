"""Stable text, JSON, and GitHub reports for aggregate repository checks."""

from __future__ import annotations

import json
from typing import Any

from yaga.commits.models import OutputFormat, ValidationReport
from yaga.commits.reporting import (
    MAX_DIAGNOSTIC_MESSAGE,
    MAX_DISPLAY_HEADER,
    SCHEMA_VERSION,
    commit_report_document,
    json_text,
    render_report,
    safe_text,
)
from yaga.errors import YagaError, safe_error_text
from yaga.repository.models import (
    RepositoryCheckResult,
    RepositoryOutputFormat,
    RepositoryReport,
)
from yaga.workflows.models import (
    WorkflowDiagnostic,
    WorkflowLintReport,
    WorkflowLintResult,
    WorkflowOutputFormat,
    WorkflowReport,
    WorkflowResult,
)
from yaga.workflows.reporting import (
    MAX_DISPLAY_PATH,
    MAX_GITHUB_TITLE,
    render_workflow_lint_report,
    render_workflow_report,
    workflow_lint_report_document,
    workflow_report_document,
)
from yaga.workflows.security_models import WorkflowSecurityReport, WorkflowSecurityResult
from yaga.workflows.security_reporting import (
    render_workflow_security_report,
    workflow_security_report_document,
)

MAX_REPOSITORY_ANNOTATIONS = 50


def render_repository_report(
    report: RepositoryReport,
    output_format: RepositoryOutputFormat,
) -> str:
    """Render one complete aggregate report."""
    if output_format is RepositoryOutputFormat.JSON:
        return json.dumps(repository_report_document(report), ensure_ascii=False, indent=2)
    if output_format is RepositoryOutputFormat.GITHUB:
        return _render_github_report(report)
    return _render_text_report(report)


def render_repository_error(
    error: YagaError,
    output_format: RepositoryOutputFormat,
) -> str:
    """Render one pre-provider invocation error."""
    message = safe_error_text(error, maximum=MAX_DIAGNOSTIC_MESSAGE)
    if output_format is RepositoryOutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "repository_check",
                "status": "error",
                "valid": False,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        )
    if output_format is RepositoryOutputFormat.GITHUB:
        title = _github_property("YAGA repository check", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"YAGA {error.kind} error: {message}",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        return f"::error title={title}::{data}"
    return f"YAGA {error.kind} error: {message}"


def repository_report_document(report: RepositoryReport) -> dict[str, Any]:
    """Return the bounded JSON-ready aggregate report."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "repository_check",
        "status": report.status.value,
        "valid": report.valid,
        "selected": report.selected,
        "passed": report.passed,
        "failed": report.failed,
        "errored": report.errored,
        "checks": [_check_document(check) for check in report.checks],
    }


def _check_document(check: RepositoryCheckResult) -> dict[str, Any]:
    error = check.error
    return {
        "provider": check.provider.value,
        "status": check.status.value,
        "report": _provider_document(check) if check.report is not None else None,
        "error": (
            {
                "kind": error.kind,
                "message": safe_error_text(error, maximum=MAX_DIAGNOSTIC_MESSAGE),
            }
            if error is not None
            else None
        ),
    }


def _provider_document(check: RepositoryCheckResult) -> dict[str, Any]:
    report = check.report
    if isinstance(report, ValidationReport):
        return commit_report_document(report)
    if isinstance(report, WorkflowLintReport):
        return workflow_lint_report_document(report)
    if isinstance(report, WorkflowSecurityReport):
        return workflow_security_report_document(report)
    if isinstance(report, WorkflowReport):
        return workflow_report_document(report)
    raise AssertionError("repository provider report has an unknown type")


def _render_text_report(report: RepositoryReport) -> str:
    lines: list[str] = []
    for check in report.checks:
        lines.append(f"== {check.provider.value} [{check.status.value.upper()}] ==")
        lines.append(_provider_text(check))
    lines.append(_summary(report, prefix="Repository checks"))
    return "\n".join(lines)


def _provider_text(check: RepositoryCheckResult) -> str:
    if check.error is not None:
        return (
            f"YAGA {check.error.kind} error: "
            f"{safe_error_text(check.error, maximum=MAX_DIAGNOSTIC_MESSAGE)}"
        )
    report = check.report
    if isinstance(report, ValidationReport):
        return render_report(report, OutputFormat.TEXT)
    if isinstance(report, WorkflowLintReport):
        return render_workflow_lint_report(report, WorkflowOutputFormat.TEXT)
    if isinstance(report, WorkflowSecurityReport):
        return render_workflow_security_report(report, WorkflowOutputFormat.TEXT)
    if isinstance(report, WorkflowReport):
        return render_workflow_report(report, WorkflowOutputFormat.TEXT)
    raise AssertionError("repository provider report has an unknown type")


def _render_github_report(report: RepositoryReport) -> str:
    lines: list[str] = []
    for check in report.checks:
        group = _github_data(
            f"YAGA repository check: {check.provider.value} ({check.status.value})",
            maximum=200,
        )
        lines.extend((f"::group::{group}", _provider_text(check), "::endgroup::"))

    annotations = [
        annotation for check in report.checks for annotation in _provider_annotations(check)
    ]
    visible = annotations
    omitted = 0
    if len(annotations) > MAX_REPOSITORY_ANNOTATIONS:
        visible = annotations[: MAX_REPOSITORY_ANNOTATIONS - 1]
        omitted = len(annotations) - len(visible)
    lines.extend(visible)
    if omitted:
        title = _github_property("YAGA repository check", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"{omitted} additional repository diagnostic(s) omitted",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        lines.append(f"::error title={title}::{data}")
    lines.append(_summary(report, prefix="YAGA repository checks"))
    return "\n".join(lines)


def _provider_annotations(check: RepositoryCheckResult) -> list[str]:
    if check.error is not None:
        title = _github_property(
            f"YAGA {check.provider.value}",
            maximum=MAX_GITHUB_TITLE,
        )
        data = _github_data(
            f"YAGA {check.error.kind} error: {safe_error_text(check.error)}",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        return [f"::error title={title}::{data}"]

    report = check.report
    if isinstance(report, ValidationReport):
        annotations: list[str] = []
        for result in report.results:
            identity = result.target.sha[:12] if result.target.sha else result.target.label
            for diagnostic in result.diagnostics:
                title = _github_property(
                    f"YAGA {diagnostic.code}",
                    maximum=MAX_GITHUB_TITLE,
                )
                data = _github_data(
                    f"{safe_text(identity, maximum=80)}: line {diagnostic.line}, "
                    f"column {diagnostic.column}: {diagnostic.message}",
                    maximum=MAX_DISPLAY_HEADER + MAX_DIAGNOSTIC_MESSAGE,
                )
                annotations.append(f"::error title={title}::{data}")
        return annotations
    if isinstance(report, WorkflowLintReport):
        return [
            _workflow_annotation(result, diagnostic)
            for result in report.results
            for diagnostic in result.diagnostics
        ]
    if isinstance(report, WorkflowSecurityReport):
        return [
            _workflow_annotation(result, diagnostic)
            for result in report.results
            for diagnostic in result.diagnostics
        ]
    if isinstance(report, WorkflowReport):
        return [
            _workflow_annotation(result, diagnostic)
            for result in report.results
            for diagnostic in result.diagnostics
        ]
    raise AssertionError("repository provider report has an unknown type")


def _workflow_annotation(
    result: WorkflowResult | WorkflowLintResult | WorkflowSecurityResult,
    diagnostic: WorkflowDiagnostic,
) -> str:
    path = _github_property(result.path, maximum=MAX_DISPLAY_PATH)
    title = _github_property(f"YAGA {diagnostic.code}", maximum=MAX_GITHUB_TITLE)
    data = _github_data(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
    return (
        f"::error file={path},line={diagnostic.line},col={diagnostic.column},title={title}::{data}"
    )


def _summary(report: RepositoryReport, *, prefix: str) -> str:
    return (
        f"{prefix}: {report.selected} selected; {report.passed} passed, "
        f"{report.failed} failed, {report.errored} errored."
    )


def _github_property(value: str, *, maximum: int) -> str:
    escaped = _github_escape(value).replace(":", "%3A").replace(",", "%2C")
    return _bounded_github_escape(escaped, maximum=maximum)


def _github_data(value: str, *, maximum: int) -> str:
    return _bounded_github_escape(_github_escape(value), maximum=maximum)


def _github_escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _bounded_github_escape(value: str, *, maximum: int) -> str:
    cleaned = json_text(value, maximum=len(value))
    if len(cleaned) <= maximum:
        return cleaned
    cutoff = maximum - 1
    last_escape = cleaned.rfind("%", 0, cutoff)
    if last_escape >= 0 and cutoff - last_escape < 3:
        cutoff = last_escape
    return f"{cleaned[:cutoff]}…"
