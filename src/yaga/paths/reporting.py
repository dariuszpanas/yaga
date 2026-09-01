"""Stable bounded reports for committed-path portability checks."""

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
from yaga.paths.models import (
    MAX_PATH_BYTES,
    MAX_PATH_REVISION_CHARS,
    PathDiagnostic,
    PathReport,
    diagnostic_code_for_rule,
)
from yaga.paths.service import CheckedPath

MAX_GITHUB_ANNOTATIONS = 32
MAX_GITHUB_TITLE = 120
MAX_TEXT_DIAGNOSTICS = 8
MAX_DISPLAY_REVISION = 300


class PathOutputFormat(StrEnum):
    """Supported committed-path policy report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


def render_path_report(checked: CheckedPath, output_format: PathOutputFormat) -> str:
    """Render one complete committed-path portability result."""
    _validate_report(checked.report)
    if output_format is PathOutputFormat.JSON:
        return json.dumps(path_report_document(checked), ensure_ascii=False, indent=2)
    if output_format is PathOutputFormat.GITHUB:
        return _render_github_report(checked)
    return _render_text_report(checked)


def render_path_error(error: YagaError, output_format: PathOutputFormat) -> str:
    """Render one expected committed-path operational failure."""
    message = safe_error_text(error, maximum=MAX_DIAGNOSTIC_MESSAGE)
    if output_format is PathOutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "path_policy",
                "status": "error",
                "valid": False,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        )
    if output_format is PathOutputFormat.GITHUB:
        title = _github_property("YAGA path policy", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"YAGA {error.kind} error: {message}",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        return f"::error title={title}::{data}"
    return f"YAGA {error.kind} error: {message}"


def path_report_document(checked: CheckedPath) -> dict[str, Any]:
    """Return the bounded schema-v1 committed-path policy document."""
    report = checked.report
    _validate_report(report)
    selection = report.selection
    by_code = {
        diagnostic_code_for_rule(rule): count
        for rule, count in report.findings_by_rule.items()
        if count
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "path_policy",
        "status": report.status,
        "valid": report.valid,
        "identity": {
            "policy_path": json_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH),
            "repository_path": json_text(str(selection.repository), maximum=MAX_DISPLAY_PATH),
            "revision": json_text(selection.revision, maximum=MAX_PATH_REVISION_CHARS),
            "commit_sha": json_text(selection.commit_sha, maximum=64),
            "tree_sha": json_text(selection.tree_sha, maximum=64),
        },
        "policy": {
            "path_policy_version": report.policy.path_policy_version,
            "profile": report.policy.profile,
            "rules": list(report.policy.rules),
        },
        "counts": {
            "paths": len(selection.paths),
            "components": sum(path.count("/") + 1 for path in selection.paths),
            "findings": report.finding_count,
            "by_code": by_code,
        },
        "diagnostics": [_diagnostic_document(item) for item in report.diagnostics],
        "diagnostics_omitted": report.diagnostics_omitted,
    }


def _diagnostic_document(diagnostic: PathDiagnostic) -> dict[str, Any]:
    return {
        "code": diagnostic.code,
        "message": json_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE),
        "path": json_text(diagnostic.path, maximum=MAX_PATH_BYTES),
        "component": diagnostic.component,
        "related_path": (
            json_text(diagnostic.related_path, maximum=MAX_PATH_BYTES)
            if diagnostic.related_path is not None
            else None
        ),
    }


def _render_text_report(checked: CheckedPath) -> str:
    report = checked.report
    selection = report.selection
    lines = [
        f"Policy: {safe_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH)}",
        f"Repository: {safe_text(str(selection.repository), maximum=MAX_DISPLAY_PATH)}",
        f"Revision: {safe_text(selection.revision, maximum=MAX_DISPLAY_REVISION)}",
        f"Commit: {safe_text(selection.commit_sha, maximum=64)}",
        f"Tree: {safe_text(selection.tree_sha, maximum=64)}",
    ]
    visible = report.diagnostics[:MAX_TEXT_DIAGNOSTICS]
    for diagnostic in visible:
        path = safe_text(diagnostic.path, maximum=MAX_DISPLAY_PATH)
        message = safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
        detail = f"FAILED  [{diagnostic.code}] {path}: {message}"
        if diagnostic.component is not None:
            detail += f" (component {diagnostic.component})"
        if diagnostic.related_path is not None:
            related = safe_text(diagnostic.related_path, maximum=MAX_DISPLAY_PATH)
            detail += f" (conflicts with: {related})"
        lines.append(detail)
    omitted = report.finding_count - len(visible)
    if omitted:
        lines.append(f"... {omitted} additional diagnostic(s) omitted")
    lines.append(_summary(report))
    return "\n".join(lines)


def _render_github_report(checked: CheckedPath) -> str:
    report = checked.report
    if report.finding_count > MAX_GITHUB_ANNOTATIONS:
        visible = report.diagnostics[: MAX_GITHUB_ANNOTATIONS - 1]
    else:
        visible = report.diagnostics[:MAX_GITHUB_ANNOTATIONS]
    lines = [_github_annotation(diagnostic) for diagnostic in visible]
    omitted = report.finding_count - len(visible)
    if omitted:
        title = _github_property("YAGA path policy", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"{omitted} additional committed-path violation(s) omitted",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        lines.append(f"::error title={title}::{data}")
    lines.append(_summary(report, prefix="YAGA path policy"))
    return "\n".join(lines)


def _github_annotation(diagnostic: PathDiagnostic) -> str:
    title = _github_property(f"YAGA {diagnostic.code}", maximum=MAX_GITHUB_TITLE)
    file_path = _github_property(diagnostic.path, maximum=MAX_DISPLAY_PATH)
    message = safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
    detail = message
    if diagnostic.component is not None:
        detail += f"; component {diagnostic.component}"
    if diagnostic.related_path is not None:
        related = safe_text(diagnostic.related_path, maximum=MAX_DISPLAY_PATH)
        detail += f"; conflicts with {related}"
    data = _github_data(detail, maximum=MAX_DIAGNOSTIC_MESSAGE)
    return f"::error file={file_path},title={title}::{data}"


def _summary(report: PathReport, *, prefix: str = "Path policy") -> str:
    return (
        f"{prefix}: {len(report.selection.paths)} path(s), "
        f"{sum(path.count('/') + 1 for path in report.selection.paths)} component(s), "
        f"{report.finding_count} finding(s)."
    )


def _validate_report(report: PathReport) -> None:
    if report.diagnostics_omitted != report.finding_count - len(report.diagnostics):
        raise AssertionError("path report omission count is inconsistent")
    expected_status = "passed" if report.finding_count == 0 else "failed"
    if report.status != expected_status or report.valid != (report.finding_count == 0):
        raise AssertionError("path report status is inconsistent")


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
