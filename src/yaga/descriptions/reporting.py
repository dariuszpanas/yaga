"""Bounded schema-v1 and escaped human reports for PR descriptions."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from yaga.descriptions.service import CheckedDescription
from yaga.errors import YagaError, safe_error_text

MAX_GITHUB_ANNOTATIONS = 50


class DescriptionOutputFormat(StrEnum):
    """Supported description report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


def description_report_document(checked: CheckedDescription) -> dict[str, Any]:
    """Return bounded metadata and findings without echoing the description."""
    report = checked.report
    return {
        "schema_version": 1,
        "kind": "pull_request_description",
        "status": report.status,
        "valid": report.valid,
        "policy_path": safe_error_text(checked.policy_path, maximum=1000),
        "source": safe_error_text(checked.source, maximum=1000),
        "template_path": (
            safe_error_text(checked.template_path, maximum=1000)
            if checked.template_path is not None
            else None
        ),
        "pull_request_number": checked.pull_request_number,
        "word_count": report.word_count,
        "character_count": report.character_count,
        "diagnostic_count": report.diagnostic_count,
        "omitted_diagnostic_count": report.omitted_diagnostic_count,
        "diagnostics": [
            {
                "code": item.code,
                "message": safe_error_text(item.message, maximum=500),
                "line": item.line,
                "column": item.column,
            }
            for item in report.diagnostics
        ],
    }


def render_description_report(
    checked: CheckedDescription, output_format: DescriptionOutputFormat
) -> str:
    """Render a complete deterministic report with bounded annotations."""
    document = description_report_document(checked)
    if output_format is DescriptionOutputFormat.JSON:
        return json.dumps(document, ensure_ascii=False, indent=2)
    report = checked.report
    summary = (
        f"PR description: {report.status}; {report.word_count} words; "
        f"{report.diagnostic_count} finding(s)."
    )
    if output_format is DescriptionOutputFormat.GITHUB:
        visible = report.diagnostics[:MAX_GITHUB_ANNOTATIONS]
        lines = [
            _annotation(f"YAGA {item.code}", f"description line {item.line}: {item.message}")
            for item in visible
        ]
    else:
        visible = report.diagnostics
        lines = [
            f"Source: {document['source']}",
            *(
                f"FAILED [{item.code}] line {item.line}: "
                f"{safe_error_text(item.message, maximum=500)}"
                for item in visible
            ),
        ]
    omitted = report.diagnostic_count - len(visible)
    if omitted:
        lines.append(f"{omitted} additional finding(s) omitted.")
    return "\n".join((*lines, summary))


def render_description_error(error: YagaError, output_format: DescriptionOutputFormat) -> str:
    """Keep operational failures distinct from policy findings."""
    message = safe_error_text(error, maximum=500)
    if output_format is DescriptionOutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": 1,
                "kind": "pull_request_description",
                "status": "error",
                "valid": False,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        )
    if output_format is DescriptionOutputFormat.GITHUB:
        return _annotation("YAGA PR description", f"{error.kind} error: {message}")
    return f"YAGA {error.kind} error: {message}"


def _annotation(title: str, message: str) -> str:
    # Sanitize first, then escape; never truncate a percent escape sequence.
    title = _escape(safe_error_text(title, maximum=120)).replace(":", "%3A").replace(",", "%2C")
    message = _escape(safe_error_text(message, maximum=500))
    return f"::error title={title}::{message}"


def _escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
