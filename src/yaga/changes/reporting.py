"""Stable bounded reports for changed-path coupling checks."""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from yaga.changes.models import (
    CHANGE_REQUIRE_ANY_CODE,
    ChangeRuleResult,
    ChangeRuleStatus,
)
from yaga.changes.service import CheckedChanges
from yaga.commits.reporting import (
    MAX_DIAGNOSTIC_MESSAGE,
    MAX_DISPLAY_PATH,
    SCHEMA_VERSION,
    json_text,
    safe_text,
)
from yaga.errors import YagaError, safe_error_text

MAX_GITHUB_ANNOTATIONS = 32
MAX_GITHUB_TITLE = 120
MAX_JSON_PATHS = 64
MAX_JSON_RULE_PATHS = 32
MAX_TEXT_PATHS = 8
MAX_DISPLAY_RANGE = 300
MAX_DISPLAY_RULE_NAME = 100
MAX_DISPLAY_PATTERN = 512


class ChangeOutputFormat(StrEnum):
    """Supported changed-path policy report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


def render_change_report(checked: CheckedChanges, output_format: ChangeOutputFormat) -> str:
    """Render one complete changed-path policy report."""
    if output_format is ChangeOutputFormat.JSON:
        return json.dumps(change_report_document(checked), ensure_ascii=False, indent=2)
    if output_format is ChangeOutputFormat.GITHUB:
        return _render_github_report(checked)
    return _render_text_report(checked)


def render_change_error(error: YagaError, output_format: ChangeOutputFormat) -> str:
    """Render one expected changed-path operational failure."""
    message = safe_error_text(error, maximum=MAX_DIAGNOSTIC_MESSAGE)
    if output_format is ChangeOutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "change_policy",
                "status": "error",
                "valid": False,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        )
    if output_format is ChangeOutputFormat.GITHUB:
        title = _github_property("YAGA change policy", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"YAGA {error.kind} error: {message}",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        return f"::error title={title}::{data}"
    return f"YAGA {error.kind} error: {message}"


def change_report_document(checked: CheckedChanges) -> dict[str, Any]:
    """Return the bounded schema-v1 changed-path policy document."""
    report = checked.report
    selection = report.selection
    paths, paths_omitted = _json_preview(
        selection.paths,
        limit=MAX_JSON_PATHS,
        maximum=MAX_DISPLAY_PATH,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "change_policy",
        "status": "passed" if report.valid else "failed",
        "valid": report.valid,
        "policy_path": json_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH),
        "range": json_text(selection.revision_range, maximum=MAX_DISPLAY_RANGE),
        "base_sha": json_text(selection.base_sha, maximum=64),
        "head_sha": json_text(selection.head_sha, maximum=64),
        "comparison_sha": json_text(selection.comparison_sha, maximum=64),
        "paths_changed": len(selection.paths),
        "paths": paths,
        "paths_omitted": paths_omitted,
        "rules": [_rule_document(result) for result in report.results],
        "passed": report.passed,
        "failed": report.failed,
        "skipped": report.skipped,
    }


def _rule_document(result: ChangeRuleResult) -> dict[str, Any]:
    triggered, triggered_omitted = _json_preview(
        result.triggered_paths,
        limit=MAX_JSON_RULE_PATHS,
        maximum=MAX_DISPLAY_PATH,
    )
    required, required_omitted = _json_preview(
        result.required_paths,
        limit=MAX_JSON_RULE_PATHS,
        maximum=MAX_DISPLAY_PATH,
    )
    failed = result.status is ChangeRuleStatus.FAILED
    code = result.code if failed else None
    if failed and code != CHANGE_REQUIRE_ANY_CODE:
        raise AssertionError("failed change rule has an unknown violation code")
    return {
        "name": json_text(result.rule.name, maximum=MAX_DISPLAY_RULE_NAME),
        "status": result.status.value,
        "code": code,
        "when_any": [
            json_text(pattern, maximum=MAX_DISPLAY_PATTERN) for pattern in result.rule.when_any
        ],
        "require_any": [
            json_text(pattern, maximum=MAX_DISPLAY_PATTERN) for pattern in result.rule.require_any
        ],
        "triggered_paths": triggered,
        "triggered_paths_omitted": triggered_omitted,
        "required_paths": required,
        "required_paths_omitted": required_omitted,
    }


def _render_text_report(checked: CheckedChanges) -> str:
    report = checked.report
    selection = report.selection
    lines = [
        f"Policy: {safe_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH)}",
        f"Range: {safe_text(selection.revision_range, maximum=MAX_DISPLAY_RANGE)}",
    ]
    for result in report.results:
        name = safe_text(result.rule.name, maximum=MAX_DISPLAY_RULE_NAME)
        lines.append(f"{result.status.value.upper():7} {name}")
        if result.status is ChangeRuleStatus.FAILED:
            patterns = _text_preview(result.rule.require_any, maximum=MAX_DISPLAY_PATTERN)
            lines.append(
                f"         [{CHANGE_REQUIRE_ANY_CODE}] no changed path matched require-any: "
                f"{patterns}"
            )
        if result.triggered_paths:
            lines.append(
                "         triggered: "
                + _text_preview(result.triggered_paths, maximum=MAX_DISPLAY_PATH)
            )
        if result.required_paths:
            lines.append(
                "         required matches: "
                + _text_preview(result.required_paths, maximum=MAX_DISPLAY_PATH)
            )
    lines.append(_summary(checked))
    return "\n".join(lines)


def _render_github_report(checked: CheckedChanges) -> str:
    annotations = [
        _github_annotation(result)
        for result in checked.report.results
        if result.status is ChangeRuleStatus.FAILED
    ]
    visible = annotations
    omitted = 0
    if len(annotations) > MAX_GITHUB_ANNOTATIONS:
        visible = annotations[: MAX_GITHUB_ANNOTATIONS - 1]
        omitted = len(annotations) - len(visible)

    lines = list(visible)
    if omitted:
        title = _github_property("YAGA change policy", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"{omitted} additional changed-path violation(s) omitted",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        lines.append(f"::error title={title}::{data}")
    lines.append(_summary(checked, prefix="YAGA change policy"))
    return "\n".join(lines)


def _github_annotation(result: ChangeRuleResult) -> str:
    if not result.triggered_paths:
        raise AssertionError("failed change rule has no triggering path")
    if result.code != CHANGE_REQUIRE_ANY_CODE:
        raise AssertionError("failed change rule has an unknown violation code")
    path = _github_property(result.triggered_paths[0], maximum=MAX_DISPLAY_PATH)
    title = _github_property(f"YAGA {CHANGE_REQUIRE_ANY_CODE}", maximum=MAX_GITHUB_TITLE)
    name = safe_text(result.rule.name, maximum=MAX_DISPLAY_RULE_NAME)
    patterns = _text_preview(result.rule.require_any, maximum=MAX_DISPLAY_PATTERN)
    data = _github_data(
        f"rule {name!r} requires at least one changed path matching: {patterns}",
        maximum=MAX_DIAGNOSTIC_MESSAGE,
    )
    return f"::error file={path},title={title}::{data}"


def _summary(checked: CheckedChanges, *, prefix: str = "Change policy") -> str:
    report = checked.report
    return (
        f"{prefix}: {len(report.results)} rule(s); {report.passed} passed, "
        f"{report.failed} failed, {report.skipped} skipped; "
        f"{len(report.selection.paths)} changed path(s)."
    )


def _json_preview(
    values: Sequence[str],
    *,
    limit: int,
    maximum: int,
) -> tuple[list[str], int]:
    visible = values[:limit]
    return (
        [json_text(value, maximum=maximum) for value in visible],
        len(values) - len(visible),
    )


def _text_preview(values: Sequence[str], *, maximum: int) -> str:
    visible = values[:MAX_TEXT_PATHS]
    rendered = ", ".join(safe_text(value, maximum=maximum) for value in visible)
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
