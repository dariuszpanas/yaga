"""Stable bounded reports for committed entry-mode policy checks."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from yaga.commits.reporting import (
    MAX_DIAGNOSTIC_MESSAGE,
    MAX_DISPLAY_PATH,
    SCHEMA_VERSION,
    json_text,
    safe_text,
)
from yaga.errors import YagaError, safe_error_text
from yaga.modes.models import (
    MAX_MODE_DIAGNOSTICS,
    MAX_MODE_PATH_BYTES,
    MAX_MODE_REVISION_CHARS,
    MODE_DISALLOWED_CODE,
    MODE_KINDS,
    ModeDiagnostic,
    ModeReport,
)
from yaga.modes.patterns import MAX_MODE_PATTERN_BYTES
from yaga.modes.service import CheckedMode

MAX_GITHUB_ANNOTATIONS = 32
MAX_GITHUB_TITLE = 120
MAX_TEXT_DIAGNOSTICS = 8
MAX_DISPLAY_REVISION = 300
MAX_DISPLAY_PATTERN = 1000


class ModeOutputFormat(StrEnum):
    """Supported committed entry-mode policy report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


def render_mode_report(checked: CheckedMode, output_format: ModeOutputFormat) -> str:
    """Render one complete committed entry-mode policy result."""
    _validate_report(checked.report)
    if output_format is ModeOutputFormat.JSON:
        return json.dumps(mode_report_document(checked), ensure_ascii=False, indent=2)
    if output_format is ModeOutputFormat.GITHUB:
        return _render_github_report(checked)
    return _render_text_report(checked)


def render_mode_error(error: YagaError, output_format: ModeOutputFormat) -> str:
    """Render one expected committed-mode operational failure."""
    message = safe_error_text(error, maximum=MAX_DIAGNOSTIC_MESSAGE)
    if output_format is ModeOutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "mode_policy",
                "status": "error",
                "valid": False,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        )
    if output_format is ModeOutputFormat.GITHUB:
        title = _github_property("YAGA mode policy", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"YAGA {error.kind} error: {message}",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        return f"::error title={title}::{data}"
    return f"YAGA {error.kind} error: {message}"


def mode_report_document(checked: CheckedMode) -> dict[str, Any]:
    """Return the bounded schema-v1 committed entry-mode policy document."""
    report = checked.report
    _validate_report(report)
    selection = report.selection
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "mode_policy",
        "status": report.status,
        "valid": report.valid,
        "identity": {
            "policy_path": json_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH),
            "repository_path": json_text(str(selection.repository), maximum=MAX_DISPLAY_PATH),
            "revision": json_text(selection.revision, maximum=MAX_MODE_REVISION_CHARS),
            "commit_sha": json_text(selection.commit_sha, maximum=64),
            "tree_sha": json_text(selection.tree_sha, maximum=64),
        },
        "policy": {
            "mode_policy_version": report.policy.mode_policy_version,
            "default_allowed_modes": [mode.value for mode in report.policy.default_allowed_modes],
            "path_overrides": [
                {
                    "pattern": json_text(override.pattern, maximum=MAX_MODE_PATTERN_BYTES),
                    "allowed_modes": [mode.value for mode in override.allowed_modes],
                }
                for override in report.policy.path_overrides
            ],
        },
        "counts": {
            "entries": len(selection.entries),
            "by_mode": _mode_counts(report),
            "findings": report.finding_count,
        },
        "diagnostics": [_diagnostic_document(item) for item in report.diagnostics],
        "diagnostics_omitted": report.diagnostics_omitted,
    }


def _diagnostic_document(diagnostic: ModeDiagnostic) -> dict[str, Any]:
    return {
        "code": diagnostic.code,
        "message": json_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE),
        "path": json_text(diagnostic.path, maximum=MAX_MODE_PATH_BYTES),
        "actual_mode": json_text(diagnostic.actual_mode, maximum=16),
        "actual_kind": diagnostic.actual_kind.value,
        "allowed_modes": [mode.value for mode in diagnostic.allowed_modes],
        "pattern": (
            json_text(diagnostic.pattern, maximum=MAX_MODE_PATTERN_BYTES)
            if diagnostic.pattern is not None
            else None
        ),
    }


def _render_text_report(checked: CheckedMode) -> str:
    report = checked.report
    selection = report.selection
    visible = report.diagnostics[:MAX_TEXT_DIAGNOSTICS]
    lines = [
        f"Policy: {safe_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH)}",
        f"Repository: {safe_text(str(selection.repository), maximum=MAX_DISPLAY_PATH)}",
        f"Revision: {safe_text(selection.revision, maximum=MAX_DISPLAY_REVISION)}",
        f"Commit: {safe_text(selection.commit_sha, maximum=64)}",
        f"Tree: {safe_text(selection.tree_sha, maximum=64)}",
    ]
    for diagnostic in visible:
        path = safe_text(diagnostic.path, maximum=MAX_DISPLAY_PATH)
        message = safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
        allowed = ", ".join(mode.value for mode in diagnostic.allowed_modes)
        detail = (
            f"FAILED  [{diagnostic.code}] {path}: {message} "
            f"(actual: {diagnostic.actual_mode} {diagnostic.actual_kind.value}; "
            f"allowed: {allowed})"
        )
        if diagnostic.pattern is not None:
            pattern = safe_text(diagnostic.pattern, maximum=MAX_DISPLAY_PATTERN)
            detail += f" (pattern: {pattern})"
        lines.append(detail)
    omitted = report.finding_count - len(visible)
    if omitted:
        lines.append(f"... {omitted} additional diagnostic(s) omitted")
    lines.append(_summary(report))
    return "\n".join(lines)


def _render_github_report(checked: CheckedMode) -> str:
    report = checked.report
    limit = (
        MAX_GITHUB_ANNOTATIONS
        if report.finding_count <= MAX_GITHUB_ANNOTATIONS
        else MAX_GITHUB_ANNOTATIONS - 1
    )
    visible = report.diagnostics[:limit]
    lines = [_github_annotation(diagnostic) for diagnostic in visible]
    omitted = report.finding_count - len(visible)
    if omitted:
        title = _github_property("YAGA mode policy", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"{omitted} additional committed-mode violation(s) omitted",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        lines.append(f"::error title={title}::{data}")
    lines.append(_summary(report, prefix="YAGA mode policy"))
    return "\n".join(lines)


def _github_annotation(diagnostic: ModeDiagnostic) -> str:
    title = _github_property(f"YAGA {diagnostic.code}", maximum=MAX_GITHUB_TITLE)
    file_path = _github_property(diagnostic.path, maximum=MAX_DISPLAY_PATH)
    message = safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
    allowed = ", ".join(mode.value for mode in diagnostic.allowed_modes)
    detail = (
        f"{message}; actual mode {diagnostic.actual_mode} "
        f"({diagnostic.actual_kind.value}); allowed modes: {allowed}"
    )
    if diagnostic.pattern is not None:
        pattern = safe_text(diagnostic.pattern, maximum=MAX_DISPLAY_PATTERN)
        detail += f"; first matching pattern: {pattern}"
    data = _github_data(detail, maximum=MAX_DIAGNOSTIC_MESSAGE)
    return f"::error file={file_path},title={title}::{data}"


def _mode_counts(report: ModeReport) -> dict[str, int]:
    return {
        kind.value: sum(entry.kind is kind for entry in report.selection.entries)
        for kind in MODE_KINDS
    }


def _summary(report: ModeReport, *, prefix: str = "Mode policy") -> str:
    counts = _mode_counts(report)
    details = ", ".join(f"{kind.value} {counts[kind.value]}" for kind in MODE_KINDS)
    return (
        f"{prefix}: {len(report.selection.entries)} entry(s); {details}; "
        f"{report.finding_count} finding(s)."
    )


def _validate_report(report: ModeReport) -> None:
    if any(diagnostic.code != MODE_DISALLOWED_CODE for diagnostic in report.diagnostics):
        raise AssertionError("mode report has an unknown diagnostic code")
    if len(report.diagnostics) != min(report.finding_count, MAX_MODE_DIAGNOSTICS):
        raise AssertionError("mode report stored-diagnostic count is inconsistent")
    if report.diagnostics_omitted != report.finding_count - len(report.diagnostics):
        raise AssertionError("mode report omission count is inconsistent")
    expected_status = "passed" if report.finding_count == 0 else "failed"
    if report.status != expected_status or report.valid != (report.finding_count == 0):
        raise AssertionError("mode report status is inconsistent")


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
