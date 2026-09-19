"""Stable human and JSON reports independent of Typer."""

from __future__ import annotations

import json
import unicodedata
from enum import StrEnum
from typing import Any

from yaga.commits.models import (
    CheckResult,
    CommitPolicy,
    LoadedConfig,
    OutputFormat,
    ValidationReport,
)
from yaga.commits.parser import parse_message
from yaga.commits.quality import MAX_REASON_LENGTH, QualityReport
from yaga.errors import YagaError, safe_error_text

SCHEMA_VERSION = 1
MAX_DISPLAY_HEADER = 160
MAX_DISPLAY_PATH = 1000
MAX_DIAGNOSTIC_MESSAGE = 500
MAX_QUALITY_GITHUB_ANNOTATIONS = 50


class QualityOutputFormat(StrEnum):
    """Supported commit-quality report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


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


def render_quality_report(
    report: QualityReport, output_format: QualityOutputFormat | OutputFormat
) -> str:
    """Render the bounded advisory model report."""
    if output_format is QualityOutputFormat.GITHUB:
        return _render_quality_github_report(report)
    document = quality_report_document(report)
    if output_format.value == OutputFormat.JSON.value:
        return json.dumps(document, ensure_ascii=False, indent=2)
    lines: list[str] = []
    for result in report.results:
        status = "FLAGGED" if result.flagged else "PASSED"
        identity = result.target.sha[:12] if result.target.sha else result.target.label
        message_lines = result.target.message.count("\n") + 1
        body_lines = _message_body_lines(result.target.message)
        line_label = "line" if message_lines == 1 else "lines"
        body_label = "body line" if body_lines == 1 else "body lines"
        message_shape = f"{body_lines} {body_label} included" if body_lines else "no body included"
        model_input_label = "title only" if report.input_mode == "title" else "full message"
        if result.input_tokens is not None and report.max_input_tokens is not None:
            coverage = f"{result.input_tokens}/{report.max_input_tokens} tokens"
            message_shape += f"; model input {coverage}"
            if result.input_truncated is True:
                message_shape += ", truncated"
            elif result.input_truncated is False:
                message_shape += ", complete"
        elif result.input_truncated is True:
            message_shape += "; model input truncated"
        elif result.input_truncated is False:
            message_shape += "; model input complete"
        character_coverage = ""
        if result.input_characters is not None:
            character_coverage = (
                f"; model input {result.input_characters}/{report.max_input_characters} chars"
            )
            if result.input_character_truncated:
                character_coverage += ", character-truncated"
        lines.append(
            f"{status:7} {safe_text(identity, maximum=80)}  "
            f"({message_lines} {line_label} checked; {message_shape}) "
            f"[model input: {model_input_label}] "
            f"{character_coverage.lstrip('; ')} "
            f"{safe_text(_message_header(result.target.message))}"
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
        if result.selected_input_sha256 is not None:
            details.append(f"input sha256: {result.selected_input_sha256}")
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
    if report.max_input_tokens is not None:
        lines.append(f"Max input tokens: {report.max_input_tokens}")
    lines.append(f"Input mode: {safe_text(report.input_mode, maximum=32)}")
    lines.append(f"Max input characters: {report.max_input_characters}")
    if report.selected_input_set_sha256 is not None:
        lines.append(f"Selected input set sha256: {report.selected_input_set_sha256}")
    return "\n".join(lines)


def render_quality_error(
    error: YagaError, output_format: QualityOutputFormat | OutputFormat
) -> str:
    """Render a quality-provider failure in the requested format."""
    if output_format is QualityOutputFormat.GITHUB:
        return f"::error title=YAGA commit quality::{_workflow_data(safe_error_text(error))}"
    return render_error(error, OutputFormat(output_format.value))


def _render_quality_github_report(report: QualityReport) -> str:
    """Render advisory findings as bounded GitHub warning annotations."""
    findings = [result for result in report.results if result.flagged]
    visible = findings[:MAX_QUALITY_GITHUB_ANNOTATIONS]
    lines = []
    for result in visible:
        label = safe_text(result.target.label, maximum=MAX_DISPLAY_PATH)
        header = safe_text(_message_header(result.target.message), maximum=MAX_DISPLAY_HEADER)
        detail = f"{label}: {header}"
        body_lines = _message_body_lines(result.target.message)
        body_label = "body line" if body_lines == 1 else "body lines"
        detail += f"; {body_lines} {body_label}"
        if result.input_characters is not None:
            detail += f"; input {result.input_characters}/{report.max_input_characters} chars"
            if result.input_character_truncated:
                detail += ", character-truncated"
        if result.input_tokens is not None and report.max_input_tokens is not None:
            detail += f"; tokens {result.input_tokens}/{report.max_input_tokens}"
            if result.input_truncated is True:
                detail += ", truncated"
            elif result.input_truncated is False:
                detail += ", complete"
        if result.assessment.score is not None:
            detail += f"; score {result.assessment.score:.3f}"
        if result.assessment.reason:
            detail += f"; {safe_text(result.assessment.reason, maximum=MAX_DIAGNOSTIC_MESSAGE)}"
        if result.selected_input_sha256 is not None:
            detail += f"; input sha256 {result.selected_input_sha256}"
        lines.append(f"::warning title=YAGA commit quality::{_workflow_data(detail)}")
    omitted = len(findings) - len(visible)
    if omitted:
        lines.append(
            f"::warning title=YAGA commit quality::{_workflow_data(f'{omitted} additional finding(s) omitted')}"
        )
    summary = (
        f"YAGA quality checked {len(report.results)} commit(s): "
        f"{len(report.results) - report.flagged} passed, {report.flagged} flagged."
    )
    metadata = (
        f"Provider: {safe_text(report.provider, maximum=MAX_DISPLAY_PATH)}; "
        f"Task: {safe_text(report.task, maximum=MAX_DISPLAY_PATH)}; "
        f"Model: {safe_text(report.model_id, maximum=MAX_DISPLAY_PATH)}"
    )
    if report.offline:
        metadata += "; Mode: offline"
    metadata += "; Input: " + ("title" if report.input_mode == "title" else "message")
    if report.revision is not None:
        metadata += f"; Revision: {safe_text(report.revision, maximum=64)}"
    if report.region is not None:
        metadata += f"; Region: {safe_text(report.region, maximum=64)}"
    if report.max_input_tokens is not None:
        metadata += f"; Max tokens: {report.max_input_tokens}"
    metadata += f"; Max chars: {report.max_input_characters}"
    character_results = [result for result in report.results if result.input_characters is not None]
    if character_results:
        character_truncated = sum(
            result.input_character_truncated is True for result in character_results
        )
        metadata += (
            f"; Character coverage: {len(character_results) - character_truncated} complete, "
            f"{character_truncated} truncated, "
            f"{len(report.results) - len(character_results)} unmeasured"
        )
    token_results = [result for result in report.results if result.input_tokens is not None]
    if token_results:
        token_truncated = sum(result.input_truncated is True for result in token_results)
        metadata += (
            f"; Token coverage: {len(token_results) - token_truncated} measured, "
            f"{token_truncated} truncated, "
            f"{len(report.results) - len(token_results)} unmeasured"
        )
    thresholds = {result.threshold for result in report.results if result.threshold is not None}
    if len(thresholds) == 1:
        metadata += f"; Threshold: {next(iter(thresholds)):.3f}"
    if report.selected_input_set_sha256 is not None:
        metadata += f"; Input set sha256: {report.selected_input_set_sha256}"
    lines.append(f"::notice title=YAGA commit quality::{_workflow_data(f'{summary} {metadata}')}")
    return "\n".join(lines)


def _workflow_data(value: str) -> str:
    """Escape workflow-command data according to the GitHub runner protocol."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


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
        "max_input_tokens": report.max_input_tokens,
        "input_mode": report.input_mode,
        "max_input_characters": report.max_input_characters,
        "selected_input_set_sha256": report.selected_input_set_sha256,
        "commits": [
            {
                "source": json_text(result.target.label, maximum=MAX_DISPLAY_PATH),
                "sha": json_text(result.target.sha, maximum=64) if result.target.sha else None,
                "message": json_text(result.target.message, maximum=MAX_DISPLAY_HEADER),
                "message_lines": result.target.message.count("\n") + 1,
                "message_body_lines": _message_body_lines(result.target.message),
                "model_input_truncated": result.input_truncated,
                "model_input_tokens": result.input_tokens,
                "model_input_characters": result.input_characters,
                "model_input_character_truncated": result.input_character_truncated,
                "selected_input_sha256": result.selected_input_sha256,
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


def _message_header(message: str) -> str:
    """Return a header even when a valid input source contains an empty message."""
    lines = message.splitlines()
    return lines[0] if lines else ""


def _message_body_lines(message: str) -> int:
    """Count parsed prose-body lines, excluding a recognized footer block."""
    parsed = parse_message(message)
    if parsed is not None:
        return len(parsed.body_lines)
    lines = message.splitlines()
    try:
        separator = lines.index("")
    except ValueError:
        return 0
    return len(lines[separator + 1 :])


def render_config(
    config: LoadedConfig,
    output_format: OutputFormat,
    *,
    dry_run: bool = False,
) -> str:
    """Render the effective configuration and its source."""
    policy = policy_document(config.policy)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "config_path": json_text(str(config.path), maximum=MAX_DISPLAY_PATH)
        if config.path
        else None,
        "config": policy,
    }
    if dry_run:
        document["dry_run"] = True
    if output_format is OutputFormat.JSON:
        return json.dumps(document, ensure_ascii=False, indent=2)
    source = (
        safe_text(str(config.path), maximum=MAX_DISPLAY_PATH)
        if config.path
        else "built-in defaults"
    )
    lines = [f"source: {source}"]
    if dry_run:
        lines.append("mode: dry-run (no file created)")
    for key, value in policy.items():
        display = "any" if value is None and key.startswith("allowed_") else value
        lines.append(f"{key.replace('_', '-')}: {_text_value(display)}")
    return "\n".join(lines)


def policy_document(policy: CommitPolicy) -> dict[str, Any]:
    """Return a JSON-ready effective policy document."""
    return {
        "config_version": policy.config_version,
        "required_version": policy.required_version,
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
        "body_paragraph_splitting": policy.body_paragraph_splitting.value,
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
            "max_input_tokens": policy.quality.max_input_tokens,
            "input_mode": policy.quality.input_mode.value,
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
                f"         [{diagnostic.code}] line {diagnostic.line}, "
                f"column {diagnostic.column}: "
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
