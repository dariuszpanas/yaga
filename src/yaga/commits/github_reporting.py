"""Stable reports for GitHub pull-request commit-policy checks."""

from __future__ import annotations

import json
from collections import Counter
from enum import StrEnum
from typing import Any

from yaga.commits.github_event import (
    DEPENDABOT_PULL_REQUEST_SKIP_REASON,
    PullRequestValidationReport,
)
from yaga.commits.models import CheckResult, OutputFormat, ValidationReport
from yaga.commits.reporting import (
    MAX_DIAGNOSTIC_MESSAGE,
    MAX_DISPLAY_HEADER,
    MAX_DISPLAY_PATH,
    SCHEMA_VERSION,
    json_text,
    render_error,
    render_report,
    result_document,
    safe_text,
)
from yaga.errors import YagaError, safe_error_text

MAX_GITHUB_ANNOTATIONS = 50
MAX_GITHUB_SUMMARY_CODES = 8


class CommitOutputFormat(StrEnum):
    """Supported standalone commit-policy report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


class PullRequestOutputFormat(StrEnum):
    """Supported pull-request report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


def render_commit_report(report: ValidationReport, output_format: CommitOutputFormat) -> str:
    """Render one standalone commit-policy report."""
    if output_format is CommitOutputFormat.GITHUB:
        return _render_commit_github_report(report)
    return render_report(report, OutputFormat(output_format.value))


def render_commit_error(error: YagaError, output_format: CommitOutputFormat) -> str:
    """Render one standalone commit-policy operational failure."""
    if output_format is CommitOutputFormat.GITHUB:
        message = _workflow_data(f"YAGA {error.kind} error: {safe_error_text(error)}")
        return f"::error title=YAGA commit policy::{message}"
    return render_error(error, OutputFormat(output_format.value))


def _render_commit_github_report(report: ValidationReport) -> str:
    diagnostics = [
        safe_text(
            f"{result.target.sha[:12] if result.target.sha else result.target.label}: "
            f"[{diagnostic.code}] line {diagnostic.line}, column {diagnostic.column}: "
            f"{diagnostic.message}",
            maximum=MAX_DISPLAY_HEADER + MAX_DIAGNOSTIC_MESSAGE,
        )
        for result in report.results
        for diagnostic in result.diagnostics
    ]
    visible = diagnostics[:MAX_GITHUB_ANNOTATIONS]
    if len(diagnostics) > MAX_GITHUB_ANNOTATIONS:
        visible[-1] = (
            f"{len(diagnostics) - MAX_GITHUB_ANNOTATIONS + 1} additional policy diagnostic(s) omitted"
        )
    lines = [f"::error title=YAGA commit policy::{_workflow_data(message)}" for message in visible]
    lines.append(
        f"YAGA checked {len(report.results)} commit(s): {report.passed} passed, "
        f"{report.failed} failed, {report.skipped} skipped."
    )
    finding_summary = _commit_finding_summary(report)
    if finding_summary is not None:
        lines.append(finding_summary)
    return "\n".join(lines)


def _commit_finding_summary(report: ValidationReport) -> str | None:
    counts = Counter(
        diagnostic.code for result in report.results for diagnostic in result.diagnostics
    )
    if not counts:
        return None
    entries = [
        f"{safe_text(code, maximum=80)} ({count})"
        for code, count in counts.most_common(MAX_GITHUB_SUMMARY_CODES)
    ]
    omitted = len(counts) - len(entries)
    if omitted:
        entries.append(f"{omitted} additional rule(s) omitted")
    return f"Findings by rule: {safe_text(', '.join(entries), maximum=1000)}"


def render_pull_request_report(
    report: PullRequestValidationReport,
    output_format: PullRequestOutputFormat,
) -> str:
    """Render one pull-request title and commit report."""
    if output_format is PullRequestOutputFormat.JSON:
        return json.dumps(_report_document(report), ensure_ascii=False, indent=2)
    if output_format is PullRequestOutputFormat.GITHUB:
        return _render_github_report(report)
    return _render_text_report(report)


def render_pull_request_error(
    error: YagaError,
    output_format: PullRequestOutputFormat,
) -> str:
    """Render an expected adapter failure in the requested format."""
    if output_format is PullRequestOutputFormat.GITHUB:
        message = _workflow_data(f"YAGA {error.kind} error: {safe_error_text(error)}")
        return f"::error title=YAGA commit policy::{message}"
    base_format = (
        OutputFormat.JSON if output_format is PullRequestOutputFormat.JSON else OutputFormat.TEXT
    )
    return render_error(error, base_format)


def _render_text_report(report: PullRequestValidationReport) -> str:
    event = report.event
    lines = [
        f"Pull request #{event.number}: {event.base_sha[:12]}..{event.head_sha[:12]}",
    ]
    lines.extend(_result_lines(report.title, kind="TITLE"))
    for result in report.commits:
        lines.extend(_result_lines(result, kind="COMMIT"))
    lines.append(
        f"Checked one title and {len(report.commits)} commit(s): {report.passed} passed, "
        f"{report.failed} failed, {report.skipped} skipped."
    )
    if report.config_path is not None:
        lines.append(f"Config: {safe_text(str(report.config_path), maximum=MAX_DISPLAY_PATH)}")
    return "\n".join(lines)


def _result_lines(result: CheckResult, *, kind: str) -> list[str]:
    identity = result.target.sha[:12] if result.target.sha else result.target.label
    lines = [
        f"{kind:<6} {result.status.upper():7} {safe_text(identity, maximum=80)}  "
        f"{safe_text(result.header)}"
    ]
    if result.skipped_reason is not None:
        lines.append(f"               skipped: {result.skipped_reason}")
    for diagnostic in result.diagnostics:
        lines.append(
            f"               [{diagnostic.code}] line {diagnostic.line}: "
            f"{safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)}"
        )
    return lines


def _report_document(report: PullRequestValidationReport) -> dict[str, Any]:
    event = report.event
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "pull_request_commit_policy",
        "valid": report.valid,
        "checked": 1 + len(report.commits),
        "passed": report.passed,
        "failed": report.failed,
        "skipped": report.skipped,
        "config_path": (
            json_text(str(report.config_path), maximum=MAX_DISPLAY_PATH)
            if report.config_path
            else None
        ),
        "pull_request": {
            "action": event.action,
            "number": event.number,
            "base_sha": event.base_sha,
            "head_sha": event.head_sha,
            "base_ref": event.base_ref,
            "head_ref": event.head_ref,
            "repository": event.repository,
            "repository_id": event.repository_id,
            "draft": event.draft,
            "author": {
                "login": event.author_login,
                "id": event.author_id,
                "type": event.author_type,
            },
            "title": result_document(report.title),
            "commits": [result_document(result) for result in report.commits],
        },
    }


def _render_github_report(report: PullRequestValidationReport) -> str:
    diagnostics: list[str] = []
    for result in report.results:
        identity = result.target.sha[:12] if result.target.sha else result.target.label
        for diagnostic in result.diagnostics:
            message = safe_text(
                f"{identity}: [{diagnostic.code}] line {diagnostic.line}, "
                f"column {diagnostic.column}: {diagnostic.message}",
                maximum=MAX_DISPLAY_HEADER + MAX_DIAGNOSTIC_MESSAGE,
            )
            diagnostics.append(message)

    lines: list[str] = []
    if len(diagnostics) > MAX_GITHUB_ANNOTATIONS:
        visible = diagnostics[: MAX_GITHUB_ANNOTATIONS - 1]
        omitted = len(diagnostics) - len(visible)
        visible.append(f"{omitted} additional policy diagnostic(s) omitted")
    else:
        visible = diagnostics
    lines.extend(
        f"::error title=YAGA commit policy::{_workflow_data(message)}" for message in visible
    )
    summary = (
        f"YAGA checked pull request #{report.event.number}: one title and "
        f"{len(report.commits)} commit(s); {report.failed} failed, "
        f"{report.skipped} skipped."
    )
    if any(
        result.skipped_reason == DEPENDABOT_PULL_REQUEST_SKIP_REASON for result in report.results
    ):
        summary += f" Skip reason: {DEPENDABOT_PULL_REQUEST_SKIP_REASON}."
    lines.append(summary)
    finding_summary = _finding_summary(report)
    if finding_summary is not None:
        lines.append(finding_summary)
    return "\n".join(lines)


def _finding_summary(report: PullRequestValidationReport) -> str | None:
    """Return a bounded rule-count summary for failed commit-policy checks."""
    counts = Counter(
        diagnostic.code for result in report.results for diagnostic in result.diagnostics
    )
    if not counts:
        return None
    entries = [
        f"{safe_text(code, maximum=80)} ({count})"
        for code, count in counts.most_common(MAX_GITHUB_SUMMARY_CODES)
    ]
    omitted = len(counts) - len(entries)
    if omitted:
        entries.append(f"{omitted} additional rule(s) omitted")
    return f"Findings by rule: {safe_text(', '.join(entries), maximum=1000)}"


def _workflow_data(value: str) -> str:
    """Escape workflow-command data according to the GitHub runner protocol."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
