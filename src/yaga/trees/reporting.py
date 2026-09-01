"""Stable bounded reports for committed-tree policy checks."""

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
from yaga.trees.models import (
    MAX_TREE_PATH_BYTES,
    MAX_TREE_REVISION_CHARS,
    TREE_FORBIDDEN_CODE,
    TREE_REQUIRED_CODE,
    TreeDiagnostic,
    TreeReport,
)
from yaga.trees.patterns import MAX_TREE_PATTERN_BYTES
from yaga.trees.service import CheckedTree

MAX_GITHUB_ANNOTATIONS = 32
MAX_GITHUB_TITLE = 120
MAX_JSON_DIAGNOSTICS = 256
MAX_TEXT_DIAGNOSTICS = 8
MAX_DISPLAY_REVISION = 300
MAX_DISPLAY_PATTERN = 1000
_TREE_CODES = frozenset({TREE_REQUIRED_CODE, TREE_FORBIDDEN_CODE})


class TreeOutputFormat(StrEnum):
    """Supported committed-tree policy report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


def render_tree_report(checked: CheckedTree, output_format: TreeOutputFormat) -> str:
    """Render one complete committed-tree policy result."""
    _validate_report(checked.report)
    if output_format is TreeOutputFormat.JSON:
        return json.dumps(tree_report_document(checked), ensure_ascii=False, indent=2)
    if output_format is TreeOutputFormat.GITHUB:
        return _render_github_report(checked)
    return _render_text_report(checked)


def render_tree_error(error: YagaError, output_format: TreeOutputFormat) -> str:
    """Render one expected committed-tree operational failure."""
    message = safe_error_text(error, maximum=MAX_DIAGNOSTIC_MESSAGE)
    if output_format is TreeOutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "tree_policy",
                "status": "error",
                "valid": False,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        )
    if output_format is TreeOutputFormat.GITHUB:
        title = _github_property("YAGA tree policy", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"YAGA {error.kind} error: {message}",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        return f"::error title={title}::{data}"
    return f"YAGA {error.kind} error: {message}"


def tree_report_document(checked: CheckedTree) -> dict[str, Any]:
    """Return the bounded schema-v1 committed-tree policy document."""
    report = checked.report
    diagnostics = _diagnostics(report)
    visible = diagnostics[:MAX_JSON_DIAGNOSTICS]
    selection = report.selection
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "tree_policy",
        "status": "passed" if report.valid else "failed",
        "valid": report.valid,
        "policy_path": json_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH),
        "repository_path": json_text(str(selection.repository), maximum=MAX_DISPLAY_PATH),
        "revision": json_text(selection.revision, maximum=MAX_TREE_REVISION_CHARS),
        "commit_sha": json_text(selection.commit_sha, maximum=64),
        "tree_sha": json_text(selection.tree_sha, maximum=64),
        "entries_checked": len(selection.paths),
        "required_paths": [
            json_text(path, maximum=MAX_TREE_PATH_BYTES) for path in report.policy.required_paths
        ],
        "forbidden_patterns": [
            json_text(pattern, maximum=MAX_TREE_PATTERN_BYTES)
            for pattern in report.policy.forbidden_patterns
        ],
        "diagnostics": [_diagnostic_document(diagnostic) for diagnostic in visible],
        "diagnostics_omitted": len(diagnostics) - len(visible),
        "missing_required": len(report.missing_required),
        "forbidden_paths": len(report.forbidden_paths),
    }


def _diagnostic_document(diagnostic: TreeDiagnostic) -> dict[str, str | None]:
    return {
        "code": diagnostic.code,
        "message": json_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE),
        "path": json_text(diagnostic.path, maximum=MAX_TREE_PATH_BYTES),
        "pattern": (
            json_text(diagnostic.pattern, maximum=MAX_TREE_PATTERN_BYTES)
            if diagnostic.pattern is not None
            else None
        ),
    }


def _render_text_report(checked: CheckedTree) -> str:
    report = checked.report
    selection = report.selection
    diagnostics = _diagnostics(report)
    lines = [
        f"Policy: {safe_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH)}",
        f"Repository: {safe_text(str(selection.repository), maximum=MAX_DISPLAY_PATH)}",
        f"Revision: {safe_text(selection.revision, maximum=MAX_DISPLAY_REVISION)}",
        f"Commit: {safe_text(selection.commit_sha, maximum=64)}",
        f"Tree: {safe_text(selection.tree_sha, maximum=64)}",
    ]
    for diagnostic in diagnostics[:MAX_TEXT_DIAGNOSTICS]:
        path = safe_text(diagnostic.path, maximum=MAX_DISPLAY_PATH)
        message = safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
        detail = f"FAILED  [{diagnostic.code}] {path}: {message}"
        if diagnostic.pattern is not None:
            pattern = safe_text(diagnostic.pattern, maximum=MAX_DISPLAY_PATTERN)
            detail += f" (pattern: {pattern})"
        lines.append(detail)
    omitted = len(diagnostics) - min(len(diagnostics), MAX_TEXT_DIAGNOSTICS)
    if omitted:
        lines.append(f"... {omitted} additional diagnostic(s) omitted")
    lines.append(_summary(report))
    return "\n".join(lines)


def _render_github_report(checked: CheckedTree) -> str:
    report = checked.report
    diagnostics = _diagnostics(report)
    if len(diagnostics) > MAX_GITHUB_ANNOTATIONS:
        visible = diagnostics[: MAX_GITHUB_ANNOTATIONS - 1]
        omitted = len(diagnostics) - len(visible)
    else:
        visible = diagnostics
        omitted = 0

    lines = [_github_annotation(diagnostic) for diagnostic in visible]
    if omitted:
        title = _github_property("YAGA tree policy", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"{omitted} additional committed-tree violation(s) omitted",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        lines.append(f"::error title={title}::{data}")
    lines.append(_summary(report, prefix="YAGA tree policy"))
    return "\n".join(lines)


def _github_annotation(diagnostic: TreeDiagnostic) -> str:
    title = _github_property(f"YAGA {diagnostic.code}", maximum=MAX_GITHUB_TITLE)
    path = safe_text(diagnostic.path, maximum=MAX_DISPLAY_PATH)
    message = safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
    if diagnostic.code == TREE_REQUIRED_CODE:
        data = _github_data(f"{path}: {message}", maximum=MAX_DIAGNOSTIC_MESSAGE)
        return f"::error title={title}::{data}"
    if diagnostic.code != TREE_FORBIDDEN_CODE or diagnostic.pattern is None:
        raise AssertionError("tree report has an invalid diagnostic")
    file_path = _github_property(diagnostic.path, maximum=MAX_DISPLAY_PATH)
    pattern = safe_text(diagnostic.pattern, maximum=MAX_DISPLAY_PATTERN)
    data = _github_data(
        f"{message}; first matching pattern: {pattern}",
        maximum=MAX_DIAGNOSTIC_MESSAGE,
    )
    return f"::error file={file_path},title={title}::{data}"


def _summary(report: TreeReport, *, prefix: str = "Tree policy") -> str:
    return (
        f"{prefix}: {len(report.selection.paths)} tree entries; "
        f"{len(report.missing_required)} missing required; "
        f"{len(report.forbidden_paths)} forbidden."
    )


def _diagnostics(report: TreeReport) -> tuple[TreeDiagnostic, ...]:
    diagnostics = tuple(report.diagnostics)
    required: list[str] = []
    forbidden: list[str] = []
    saw_forbidden = False
    for diagnostic in diagnostics:
        if diagnostic.code not in _TREE_CODES:
            raise AssertionError("tree report has an unknown diagnostic code")
        if diagnostic.code == TREE_REQUIRED_CODE:
            if saw_forbidden or diagnostic.pattern is not None:
                raise AssertionError("tree report has an invalid required-path diagnostic")
            required.append(diagnostic.path)
            continue
        saw_forbidden = True
        if diagnostic.pattern is None:
            raise AssertionError("tree report has an invalid forbidden-path diagnostic")
        forbidden.append(diagnostic.path)
    if tuple(required) != tuple(report.missing_required):
        raise AssertionError("tree report missing-required summary is inconsistent")
    if tuple(forbidden) != tuple(report.forbidden_paths):
        raise AssertionError("tree report forbidden-path summary is inconsistent")
    if report.valid != (not diagnostics):
        raise AssertionError("tree report validity is inconsistent")
    return diagnostics


def _validate_report(report: TreeReport) -> None:
    diagnostics = _diagnostics(report)
    expected_status = "passed" if not diagnostics else "failed"
    if str(report.status) != expected_status:
        raise AssertionError("tree report status is inconsistent")


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
