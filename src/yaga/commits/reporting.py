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
from yaga.errors import YagaError, safe_error_text

SCHEMA_VERSION = 1
MAX_DISPLAY_HEADER = 160
MAX_DISPLAY_PATH = 1000
MAX_DIAGNOSTIC_MESSAGE = 500


def render_report(report: ValidationReport, output_format: OutputFormat) -> str:
    """Render one complete validation report."""
    if output_format is OutputFormat.JSON:
        return json.dumps(_report_document(report), ensure_ascii=False, indent=2)
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


def render_config(config: LoadedConfig, output_format: OutputFormat) -> str:
    """Render the effective configuration and its source."""
    policy = policy_document(config.policy)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "config_path": _json_text(str(config.path), maximum=MAX_DISPLAY_PATH)
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
        "allowed_scopes": (_json_values(policy.allowed_scopes, maximum=128)),
        "scope_case": policy.scope_case.value,
        "header_max_length": policy.header_max_length,
        "description_min_length": policy.description_min_length,
        "description_max_length": policy.description_max_length,
        "description_ending": policy.description_ending.value,
        "body_policy": policy.body_policy.value,
        "body_min_length": policy.body_min_length,
        "body_max_line_length": policy.body_max_line_length,
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


def _report_document(report: ValidationReport) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "valid": report.valid,
        "checked": len(report.results),
        "passed": report.passed,
        "failed": report.failed,
        "skipped": report.skipped,
        "config_path": (
            _json_text(str(report.config_path), maximum=MAX_DISPLAY_PATH)
            if report.config_path
            else None
        ),
        "commits": [_result_document(result) for result in report.results],
    }


def _result_document(result: CheckResult) -> dict[str, Any]:
    return {
        "source": _json_text(result.target.label, maximum=MAX_DISPLAY_PATH),
        "sha": _json_text(result.target.sha, maximum=64) if result.target.sha else None,
        "header": _json_text(result.header, maximum=MAX_DISPLAY_HEADER),
        "status": result.status,
        "valid": result.valid,
        "skipped_reason": (
            _json_text(result.skipped_reason, maximum=100) if result.skipped_reason else None
        ),
        "diagnostics": [
            {
                "code": diagnostic.code,
                "message": _json_text(
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
    if isinstance(value, list):
        return safe_text(", ".join(str(item) for item in value), maximum=2000) if value else "none"
    return safe_text(str(value), maximum=2000)


def _json_values(values: tuple[str, ...] | None, *, maximum: int) -> list[str] | None:
    if values is None:
        return None
    return [_json_text(value, maximum=maximum) for value in values]


def _json_text(value: str, *, maximum: int) -> str:
    cleaned = "".join(
        "?" if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char for char in value
    )
    if len(cleaned) <= maximum:
        return cleaned
    return f"{cleaned[: maximum - 1]}…"
