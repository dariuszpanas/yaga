"""Deterministic text, JSON, and GitHub Actions rendering for policy init and policy check."""

from __future__ import annotations

import json
from enum import StrEnum

from yaga.errors import YagaError, safe_error_text
from yaga.policies.audit import PolicyAuditReport
from yaga.policies.init_templates import PolicyInitReport


class PolicyOutputFormat(StrEnum):
    """Supported output formats for yaga policy check."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


class PolicyInitOutputFormat(StrEnum):
    """Supported output formats for yaga policy init."""

    TEXT = "text"
    JSON = "json"


def render_policy_init(report: PolicyInitReport, output_format: PolicyInitOutputFormat) -> str:
    """Render yaga policy init results."""
    if output_format is PolicyInitOutputFormat.JSON:
        payload = {
            "schema_version": 1,
            "kind": "policy_init",
            "repository": str(report.repository),
            "dry_run": report.dry_run,
            "created": sum(1 for item in report.items if item.status == "created"),
            "previewed": sum(1 for item in report.items if item.status == "dry-run"),
            "skipped": sum(1 for item in report.items if item.status == "skipped"),
            "policies": [
                {
                    "kind": item.kind.value,
                    "path": str(item.path),
                    "schema_version": item.schema_version,
                    "status": item.status,
                }
                for item in report.items
            ],
        }
        return json.dumps(payload, indent=2)

    lines: list[str] = []
    mode_note = "dry-run (no files created)" if report.dry_run else "applied"
    lines.append(f"Repository: {safe_error_text(report.repository, maximum=300)} ({mode_note})")
    for item in report.items:
        badge = {
            "created": "CREATED",
            "dry-run": "PREVIEW",
            "skipped": "SKIPPED",
        }[item.status]
        safe_path = safe_error_text(item.path, maximum=300)
        lines.append(f"{badge:<7}  {item.kind.value:<13}  v{item.schema_version}  {safe_path}")
    return "\n".join(lines)


def render_policy_audit(report: PolicyAuditReport, output_format: PolicyOutputFormat) -> str:
    """Render yaga policy check results."""
    if output_format is PolicyOutputFormat.JSON:
        status = (
            "error" if report.invalid_count > 0 else ("failed" if not report.valid else "passed")
        )
        payload = {
            "schema_version": 1,
            "kind": "policy_check",
            "status": status,
            "valid": report.valid,
            "strict": report.strict,
            "checked": len(report.entries),
            "current": report.current_count,
            "deprecated": report.deprecated_count,
            "invalid": report.invalid_count,
            "policies": [
                {
                    "path": str(entry.path),
                    "kind": entry.kind,
                    "version_key": entry.version_key,
                    "source_version": entry.source_version,
                    "current_version": entry.current_version,
                    "required_version": entry.required_version,
                    "status": entry.status,
                    "message": entry.message,
                }
                for entry in report.entries
            ],
        }
        return json.dumps(payload, indent=2)

    if output_format is PolicyOutputFormat.GITHUB:
        annotations: list[str] = []
        for entry in report.entries:
            safe_path = _escape_github_property(safe_error_text(entry.path, maximum=300))
            safe_msg = _escape_github_data(
                f"[{entry.kind}] {safe_error_text(entry.message, maximum=400)}"
            )
            if entry.status == "invalid":
                annotations.append(f"::error title=YAGA policy,file={safe_path}::{safe_msg}")
            elif entry.status == "deprecated":
                level = "error" if report.strict else "warning"
                annotations.append(f"::{level} title=YAGA policy,file={safe_path}::{safe_msg}")
        summary = (
            f"Checked {len(report.entries)} policy section(s): "
            f"{report.current_count} current, {report.deprecated_count} deprecated, "
            f"{report.invalid_count} invalid."
        )
        annotations.append(summary)
        return "\n".join(annotations)

    lines = []
    for entry in report.entries:
        badge = {
            "current": "CURRENT",
            "deprecated": "DEPRECATED",
            "invalid": "INVALID",
        }[entry.status]
        safe_path = safe_error_text(entry.path, maximum=300)
        version_text = (
            f"v{entry.source_version}/v{entry.current_version}"
            if entry.source_version is not None and entry.current_version is not None
            else "-"
        )
        lines.append(
            f"{badge:<10}  {entry.kind:<13}  {version_text:<7}  {safe_path}  ({entry.message})"
        )
    lines.append(
        f"Checked {len(report.entries)} policy section(s): "
        f"{report.current_count} current, {report.deprecated_count} deprecated, "
        f"{report.invalid_count} invalid."
    )
    return "\n".join(lines)


def render_policy_error(
    error: YagaError,
    output_format: PolicyOutputFormat | PolicyInitOutputFormat,
) -> str:
    """Render an operational error in the selected format."""
    message = safe_error_text(error, maximum=500)
    if output_format in (PolicyOutputFormat.JSON, PolicyInitOutputFormat.JSON):
        return json.dumps(
            {
                "schema_version": 1,
                "kind": "policy_check",
                "status": "error",
                "valid": False,
                "error": {
                    "kind": error.kind,
                    "message": message,
                },
            },
            indent=2,
        )
    if output_format is PolicyOutputFormat.GITHUB:
        return f"::error title=YAGA {error.kind} error::{_escape_github_data(message)}"
    return f"YAGA {error.kind} error: {message}"


def _escape_github_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_github_property(value: str) -> str:
    return _escape_github_data(value).replace(":", "%3A").replace(",", "%2C")
