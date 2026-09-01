"""Stable bounded reports for explicit branch-name policy checks."""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from yaga.branches.models import (
    BRANCH_ALLOWED_CODE,
    BRANCH_SYNTAX_CODE,
    MAX_BRANCH_NAME_BYTES,
    BranchDiagnostic,
)
from yaga.branches.service import CheckedBranch
from yaga.commits.reporting import (
    MAX_DIAGNOSTIC_MESSAGE,
    MAX_DISPLAY_PATH,
    SCHEMA_VERSION,
    json_text,
    safe_text,
)
from yaga.errors import YagaError, safe_error_text

MAX_DISPLAY_PATTERN = MAX_BRANCH_NAME_BYTES
MAX_GITHUB_TITLE = 120
MAX_TEXT_PATTERNS = 8
_BRANCH_CODES = frozenset({BRANCH_SYNTAX_CODE, BRANCH_ALLOWED_CODE})


class BranchOutputFormat(StrEnum):
    """Supported branch-policy report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


def render_branch_report(checked: CheckedBranch, output_format: BranchOutputFormat) -> str:
    """Render one complete branch-policy result."""
    if output_format is BranchOutputFormat.JSON:
        return json.dumps(branch_report_document(checked), ensure_ascii=False, indent=2)
    if output_format is BranchOutputFormat.GITHUB:
        return _render_github_report(checked)
    return _render_text_report(checked)


def render_branch_error(error: YagaError, output_format: BranchOutputFormat) -> str:
    """Render one expected branch-policy operational failure."""
    message = safe_error_text(error, maximum=MAX_DIAGNOSTIC_MESSAGE)
    if output_format is BranchOutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "branch_policy",
                "status": "error",
                "valid": False,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        )
    if output_format is BranchOutputFormat.GITHUB:
        title = _github_property("YAGA branch policy", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"YAGA {error.kind} error: {message}",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        return f"::error title={title}::{data}"
    return f"YAGA {error.kind} error: {message}"


def branch_report_document(checked: CheckedBranch) -> dict[str, Any]:
    """Return the bounded schema-v1 branch-policy document."""
    report = checked.report
    diagnostics = [_diagnostic_document(item) for item in _diagnostics(report.diagnostics)]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "branch_policy",
        "status": str(report.status),
        "valid": report.valid,
        "policy_path": json_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH),
        "branch": json_text(report.branch, maximum=MAX_BRANCH_NAME_BYTES),
        "allowed_patterns": [
            json_text(pattern, maximum=MAX_DISPLAY_PATTERN)
            for pattern in report.policy.allowed_patterns
        ],
        "matched_pattern": (
            json_text(report.matched_pattern, maximum=MAX_DISPLAY_PATTERN)
            if report.matched_pattern is not None
            else None
        ),
        "diagnostics": diagnostics,
    }


def _diagnostic_document(diagnostic: BranchDiagnostic) -> dict[str, str]:
    return {
        "code": diagnostic.code,
        "message": json_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE),
    }


def _render_text_report(checked: CheckedBranch) -> str:
    report = checked.report
    lines = [
        f"Policy: {safe_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH)}",
        f"Branch: {safe_text(report.branch, maximum=MAX_BRANCH_NAME_BYTES)}",
    ]
    if report.matched_pattern is not None:
        lines.append(f"Matched: {safe_text(report.matched_pattern, maximum=MAX_DISPLAY_PATTERN)}")
    else:
        diagnostic = _one_diagnostic(report.diagnostics)
        lines.extend(
            (
                f"FAILED  [{diagnostic.code}] "
                f"{safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)}",
                f"Allowed: {_text_preview(report.policy.allowed_patterns)}",
            )
        )
    lines.append(f"Branch policy: {str(report.status)}.")
    return "\n".join(lines)


def _render_github_report(checked: CheckedBranch) -> str:
    report = checked.report
    branch = safe_text(report.branch, maximum=MAX_BRANCH_NAME_BYTES)
    if report.valid:
        if report.matched_pattern is None:
            raise AssertionError("valid branch report has no matched pattern")
        pattern = safe_text(report.matched_pattern, maximum=MAX_DISPLAY_PATTERN)
        return f"YAGA branch policy: passed; branch {branch!r} matched {pattern!r}."

    diagnostic = _one_diagnostic(report.diagnostics)
    title = _github_property(f"YAGA {diagnostic.code}", maximum=MAX_GITHUB_TITLE)
    data = _github_data(
        f"branch {branch!r}: {diagnostic.message}",
        maximum=MAX_DIAGNOSTIC_MESSAGE,
    )
    summary = f"YAGA branch policy: failed for branch {branch!r}."
    return f"::error title={title}::{data}\n{summary}"


def _diagnostics(values: Sequence[BranchDiagnostic]) -> tuple[BranchDiagnostic, ...]:
    diagnostics = tuple(values)
    if len(diagnostics) > 1 or any(item.code not in _BRANCH_CODES for item in diagnostics):
        raise AssertionError("branch report has an invalid diagnostic set")
    return diagnostics


def _one_diagnostic(values: Sequence[BranchDiagnostic]) -> BranchDiagnostic:
    diagnostics = _diagnostics(values)
    if len(diagnostics) != 1:
        raise AssertionError("failed branch report must have exactly one diagnostic")
    return diagnostics[0]


def _text_preview(values: Sequence[str]) -> str:
    visible = values[:MAX_TEXT_PATTERNS]
    rendered = ", ".join(safe_text(value, maximum=MAX_DISPLAY_PATTERN) for value in visible)
    omitted = len(values) - len(visible)
    if omitted:
        return f"{rendered} … (+{omitted} more)"
    return rendered


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
