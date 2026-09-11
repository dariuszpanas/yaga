"""Stable human and JSON reports independent of Typer."""

from __future__ import annotations

import json
import unicodedata
from typing import Any

from yaga.commits.models import (
    CheckResult,
    CommitPolicy,
    LoadedConfig,
    OutputFormat,
    ValidationReport,
)
from yaga.commits.quality import MAX_REASON_LENGTH, QualityReport
from yaga.errors import YagaError, safe_error_text

SCHEMA_VERSION = 1
MAX_DISPLAY_HEADER = 160
MAX_DISPLAY_PATH = 1000
MAX_DIAGNOSTIC_MESSAGE = 500


def render_report(report: ValidationReport, output_format: OutputFormat) -> str:
    """Render one complete validation report."""
    if output_format is OutputFormat.JSON:
        return json.dumps(commit_report_document(report), ensure_ascii=False, indent=2)
    return _render_text_report(report)


def render_error(error: YagaError, output_format: OutputFormat) -> str:
    """Render an expected operational failure without control sequences."""
    message = safe_error_text(error)
    if output_format is OutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        )
    return f"YAGA {error.kind} error: {message}"


def render_quality_report(report: QualityReport, output_format: OutputFormat) -> str:
    """Render the bounded advisory model report."""
    document = quality_report_document(report)
    if output_format is OutputFormat.JSON:
        return json.dumps(document, ensure_ascii=False, indent=2)
    lines: list[str] = []
    for result in report.results:
        status = "FLAGGED" if result.flagged else "PASSED"
        identity = result.target.sha[:12] if result.target.sha else result.target.label
        message_lines = result.target.message.count("\n") + 1
        line_label = "line" if message_lines == 1 else "lines"
        lines.append(
            f"{status:7} {safe_text(identity, maximum=80)}  "
            f"({message_lines} {line_label} checked) "
            f"{safe_text(result.target.message.splitlines()[0])}"
        )
        details = []
        if result.assessment.score is not None:
            details.append(f"score: {result.assessment.score:.3f}")
        if result.threshold is not None:
            details.append(f"threshold: {result.threshold:.3f}")
        if result.assessment.reason:
            details.append(
                f"reason: {safe_text(result.assessment.reason, maximum=MAX_DIAGNOSTIC_MESSAGE)}"
            )
        if details:
            lines.append(f"         {'; '.join(details)}")
    lines.append(
        f"Checked {len(report.results)} commit(s): {len(report.results) - report.flagged} passed, "
        f"{report.flagged} flagged."
    )
    lines.append(f"Provider: {safe_text(report.provider, maximum=MAX_DISPLAY_PATH)}")
    lines.append(f"Task: {safe_text(report.task, maximum=MAX_DISPLAY_PATH)}")
    lines.append(f"Model: {safe_text(report.model_id, maximum=MAX_DISPLAY_PATH)}")
    if report.revision is not None:
        lines.append(f"Revision: {safe_text(report.revision, maximum=MAX_DISPLAY_PATH)}")
    if report.region is not None:
        lines.append(f"Region: {safe_text(report.region, maximum=MAX_DISPLAY_PATH)}")
    if report.offline:
        lines.append("Mode: offline")
    return "\n".join(lines)


def quality_report_document(report: QualityReport) -> dict[str, Any]:
    """Return the versioned JSON-ready model report."""
    return {
        "schema_version": SCHEMA_VERSION,
        "valid": report.valid,
        "checked": len(report.results),
        "flagged": report.flagged,
        "provider": report.provider,
        "task": report.task,
        "model": {"id": json_text(report.model_id, maximum=256), "revision": report.revision},
        "region": report.region,
        "offline": report.offline,
        "commits": [
            {
                "source": json_text(result.target.label, maximum=MAX_DISPLAY_PATH),
                "sha": json_text(result.target.sha, maximum=64) if result.target.sha else None,
                "message": json_text(result.target.message, maximum=MAX_DISPLAY_HEADER),
                "message_lines": result.target.message.count("\n") + 1,
                "status": "flagged" if result.flagged else "passed",
                "score": result.assessment.score,
                "reason": json_text(result.assessment.reason, maximum=MAX_REASON_LENGTH)
                if result.assessment.reason is not None
                else None,
                "threshold": result.threshold,
            }
            for result in report.results
        ],
    }


def render_config(config: LoadedConfig, output_format: OutputFormat) -> str:
    """Render the effective configuration and its source."""
    policy = policy_document(config.policy)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "config_path": json_text(str(config.path), maximum=MAX_DISPLAY_PATH)
        if config.path
        else None,
        "config": policy,
    }
    if output_format is OutputFormat.JSON:
        return json.dumps(document, ensure_ascii=False, indent=2)
    source = (
        safe_text(str(config.path), maximum=MAX_DISPLAY_PATH)
        if config.path
        else "built-in defaults"
    )
    lines = [f"source: {source}"]
    for key, value in policy.items():
        display = "any" if value is None and key.startswith("allowed_") else value
        lines.append(f"{key.replace('_', '-')}: {_text_value(display)}")
    return "\n".join(lines)


def policy_document(policy: CommitPolicy) -> dict[str, Any]:
    """Return a JSON-ready effective policy document."""
    return {
        "config_version": policy.config_version,
        "allowed_types": _json_values(policy.allowed_types, maximum=128),
        "type_case": policy.type_case.value,
        "scope_policy": policy.scope_policy.value,
        "scope_policy_by_type": {
            commit_type: scope_policy.value
            for commit_type, scope_policy in policy.scope_policy_by_type
        },
        "allowed_scopes": (_json_values(policy.allowed_scopes, maximum=128)),
        "scope_case": policy.scope_case.value,
        "header_max_length": policy.header_max_length,
        "description_min_length": policy.description_min_length,
        "description_max_length": policy.description_max_length,
        "description_ending": policy.description_ending.value,
        "breaking_markers": policy.breaking_markers.value,
        "required_footer_tokens": _json_values(policy.required_footer_tokens, maximum=128),
        "forbidden_footer_tokens": _json_values(policy.forbidden_footer_tokens, maximum=128),
        "body_policy": policy.body_policy.value,
        "body_min_length": policy.body_min_length,
        "body_min_words": policy.body_min_words,
        "body_max_line_length": policy.body_max_line_length,
        "body_max_consecutive_single_line_paragraphs": policy.body_max_consecutive_single_line_paragraphs,
        "dependabot_pull_requests": policy.dependabot_pull_requests.value,
        "typos": policy.typos.value,
        "quality": {
            "provider": policy.quality.provider.value,
            "task": policy.quality.task.value,
            "model_id": json_text(policy.quality.model_id, maximum=256),
            "revision": policy.quality.revision,
            "threshold": policy.quality.threshold,
            "region": policy.quality.region,
            "max_tokens": policy.quality.max_tokens,
        },
        "merge_commits": policy.merge_commits.value,
        "ignored_headers": _json_values(policy.ignored_headers, maximum=256),
        "max_commits": policy.max_commits,
    }


def safe_text(value: str, *, maximum: int = MAX_DISPLAY_HEADER) -> str:
    """Collapse whitespace, remove terminal controls, and bound displayed input."""
    cleaned = "".join(
        "?" if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char for char in value
    )
    collapsed = " ".join(cleaned.split())
    if len(collapsed) <= maximum:
        return collapsed
    return f"{collapsed[: maximum - 1]}…"


def _render_text_report(report: ValidationReport) -> str:
    lines: list[str] = []
    for result in report.results:
        identity = result.target.sha[:12] if result.target.sha else result.target.label
        lines.append(
            f"{result.status.upper():7} {safe_text(identity, maximum=80)}  "
            f"{safe_text(result.header)}"
        )
        if result.skipped_reason is not None:
            lines.append(f"         skipped: {result.skipped_reason}")
        for diagnostic in result.diagnostics:
            lines.append(
                f"         [{diagnostic.code}] line {diagnostic.line}: "
                f"{safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)}"
            )
    lines.append(
        f"Checked {len(report.results)} commit(s): {report.passed} passed, "
        f"{report.failed} failed, {report.skipped} skipped."
    )
    if report.config_path is not None:
        lines.append(f"Config: {safe_text(str(report.config_path), maximum=MAX_DISPLAY_PATH)}")
    return "\n".join(lines)


def commit_report_document(report: ValidationReport) -> dict[str, Any]:
    """Return the bounded JSON-ready Conventional Commit report."""
    return {
        "schema_version": SCHEMA_VERSION,
        "valid": report.valid,
        "checked": len(report.results),
        "passed": report.passed,
        "failed": report.failed,
        "skipped": report.skipped,
        "config_path": (
            json_text(str(report.config_path), maximum=MAX_DISPLAY_PATH)
            if report.config_path
            else None
        ),
        "commits": [result_document(result) for result in report.results],
    }


def result_document(result: CheckResult) -> dict[str, Any]:
    """Return the bounded JSON representation of one check result."""
    return {
        "source": json_text(result.target.label, maximum=MAX_DISPLAY_PATH),
        "sha": json_text(result.target.sha, maximum=64) if result.target.sha else None,
        "header": json_text(result.header, maximum=MAX_DISPLAY_HEADER),
        "status": result.status,
        "valid": result.valid,
        "skipped_reason": (
            json_text(result.skipped_reason, maximum=100) if result.skipped_reason else None
        ),
        "diagnostics": [
            {
                "code": diagnostic.code,
                "message": json_text(
                    diagnostic.message,
                    maximum=MAX_DIAGNOSTIC_MESSAGE,
                ),
                "line": diagnostic.line,
                "column": diagnostic.column,
            }
            for diagnostic in result.diagnostics
        ],
    }


def _text_value(value: object) -> str:
    if value is None:
        return "unlimited"
    if isinstance(value, dict):
        if not value:
            return "none"
        return safe_text(
            ", ".join(f"{key}={item}" for key, item in value.items()),
            maximum=2000,
        )
    if isinstance(value, list):
        return safe_text(", ".join(str(item) for item in value), maximum=2000) if value else "none"
    return safe_text(str(value), maximum=2000)


def _json_values(values: tuple[str, ...] | None, *, maximum: int) -> list[str] | None:
    if values is None:
        return None
    return [json_text(value, maximum=maximum) for value in values]


def json_text(value: str, *, maximum: int) -> str:
    """Return bounded control-sanitized text without collapsing JSON whitespace."""
    cleaned = "".join(
        "?" if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char for char in value
    )
    if len(cleaned) <= maximum:
        return cleaned
    return f"{cleaned[: maximum - 1]}…"
