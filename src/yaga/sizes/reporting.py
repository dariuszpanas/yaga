"""Stable bounded reports for committed blob-size policy checks."""

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
from yaga.sizes.models import (
    MAX_SIZE_PATH_BYTES,
    MAX_SIZE_REVISION_CHARS,
    SIZE_BLOB_CODE,
    SIZE_TOTAL_CODE,
    BlobEntry,
    SizeDiagnostic,
    SizeReport,
)
from yaga.sizes.patterns import MAX_SIZE_PATTERN_BYTES
from yaga.sizes.service import CheckedSize

MAX_GITHUB_ANNOTATIONS = 32
MAX_GITHUB_TITLE = 120
MAX_JSON_DIAGNOSTICS = 256
MAX_JSON_LARGEST = 32
MAX_TEXT_DIAGNOSTICS = 8
MAX_DISPLAY_REVISION = 300
MAX_DISPLAY_PATTERN = 1000
_SIZE_CODES = frozenset({SIZE_BLOB_CODE, SIZE_TOTAL_CODE})


class SizeOutputFormat(StrEnum):
    """Supported committed blob-size policy report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


def render_size_report(checked: CheckedSize, output_format: SizeOutputFormat) -> str:
    """Render one complete committed blob-size policy result."""
    _validate_report(checked.report)
    if output_format is SizeOutputFormat.JSON:
        return json.dumps(size_report_document(checked), ensure_ascii=False, indent=2)
    if output_format is SizeOutputFormat.GITHUB:
        return _render_github_report(checked)
    return _render_text_report(checked)


def render_size_error(error: YagaError, output_format: SizeOutputFormat) -> str:
    """Render one expected committed blob-size operational failure."""
    message = safe_error_text(error, maximum=MAX_DIAGNOSTIC_MESSAGE)
    if output_format is SizeOutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "size_policy",
                "status": "error",
                "valid": False,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        )
    if output_format is SizeOutputFormat.GITHUB:
        title = _github_property("YAGA size policy", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"YAGA {error.kind} error: {message}",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        return f"::error title={title}::{data}"
    return f"YAGA {error.kind} error: {message}"


def size_report_document(checked: CheckedSize) -> dict[str, Any]:
    """Return the bounded schema-v1 committed blob-size policy document."""
    report = checked.report
    _validate_report(report)
    selection = report.selection
    diagnostics = tuple(report.diagnostics)
    visible_diagnostics = _visible_with_aggregate(
        diagnostics,
        limit=MAX_JSON_DIAGNOSTICS,
    )
    largest_entries = tuple(
        sorted(selection.blobs, key=lambda blob: (-blob.size, blob.path))[:MAX_JSON_LARGEST]
    )
    largest = [_largest_document(report, blob) for blob in largest_entries]
    total_limit = report.policy.max_total_blob_bytes
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "size_policy",
        "status": report.status,
        "valid": report.valid,
        "identity": {
            "policy_path": json_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH),
            "repository_path": json_text(str(selection.repository), maximum=MAX_DISPLAY_PATH),
            "revision": json_text(selection.revision, maximum=MAX_SIZE_REVISION_CHARS),
            "commit_sha": json_text(selection.commit_sha, maximum=64),
            "tree_sha": json_text(selection.tree_sha, maximum=64),
        },
        "policy": {
            "size_policy_version": report.policy.size_policy_version,
            "default_max_blob_bytes": report.policy.default_max_blob_bytes,
            "max_total_blob_bytes": total_limit,
            "path_limits": [
                {
                    "pattern": json_text(item.pattern, maximum=MAX_SIZE_PATTERN_BYTES),
                    "max_blob_bytes": item.max_blob_bytes,
                }
                for item in report.policy.path_limits
            ],
        },
        "counts": {
            "blobs": len(selection.blobs),
            "gitlinks": len(selection.gitlinks),
            "oversized_blobs": len(report.oversized_blobs),
        },
        "total": {
            "blob_bytes": report.total_bytes,
            "max_total_blob_bytes": total_limit,
            "exceeded": report.total_exceeded,
        },
        "largest": {
            "blobs": largest,
            "omitted": len(selection.blobs) - len(largest),
        },
        "diagnostics": [_diagnostic_document(diagnostic) for diagnostic in visible_diagnostics],
        "diagnostics_omitted": len(diagnostics) - len(visible_diagnostics),
    }


def _diagnostic_document(diagnostic: SizeDiagnostic) -> dict[str, Any]:
    return {
        "code": diagnostic.code,
        "message": json_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE),
        "path": (
            json_text(diagnostic.path, maximum=MAX_SIZE_PATH_BYTES)
            if diagnostic.path is not None
            else None
        ),
        "size_bytes": diagnostic.size,
        "max_bytes": diagnostic.limit,
        "pattern": (
            json_text(diagnostic.pattern, maximum=MAX_SIZE_PATTERN_BYTES)
            if diagnostic.pattern is not None
            else None
        ),
    }


def _largest_document(report: SizeReport, blob: BlobEntry) -> dict[str, Any]:
    limit, pattern = report._selected_limit_for_path(blob.path)
    return {
        "path": json_text(blob.path, maximum=MAX_SIZE_PATH_BYTES),
        "oid": json_text(blob.oid, maximum=64),
        "mode": blob.mode,
        "size_bytes": blob.size,
        "max_blob_bytes": limit,
        "pattern": (
            json_text(pattern, maximum=MAX_SIZE_PATTERN_BYTES) if pattern is not None else None
        ),
    }


def _render_text_report(checked: CheckedSize) -> str:
    report = checked.report
    selection = report.selection
    diagnostics = _visible_with_aggregate(report.diagnostics, limit=MAX_TEXT_DIAGNOSTICS)
    lines = [
        f"Policy: {safe_text(str(checked.policy_path), maximum=MAX_DISPLAY_PATH)}",
        f"Repository: {safe_text(str(selection.repository), maximum=MAX_DISPLAY_PATH)}",
        f"Revision: {safe_text(selection.revision, maximum=MAX_DISPLAY_REVISION)}",
        f"Commit: {safe_text(selection.commit_sha, maximum=64)}",
        f"Tree: {safe_text(selection.tree_sha, maximum=64)}",
    ]
    for diagnostic in diagnostics:
        message = safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
        if diagnostic.path is None:
            lines.append(
                f"FAILED  [{diagnostic.code}] {message}: "
                f"{diagnostic.size} > {diagnostic.limit} bytes"
            )
            continue
        path = safe_text(diagnostic.path, maximum=MAX_DISPLAY_PATH)
        detail = (
            f"FAILED  [{diagnostic.code}] {path}: {message} "
            f"({diagnostic.size} > {diagnostic.limit} bytes)"
        )
        if diagnostic.pattern is not None:
            pattern = safe_text(diagnostic.pattern, maximum=MAX_DISPLAY_PATTERN)
            detail += f" (pattern: {pattern})"
        lines.append(detail)
    omitted = len(report.diagnostics) - len(diagnostics)
    if omitted:
        lines.append(f"... {omitted} additional diagnostic(s) omitted")
    lines.append(_summary(report))
    return "\n".join(lines)


def _render_github_report(checked: CheckedSize) -> str:
    report = checked.report
    diagnostics = _visible_with_aggregate(
        report.diagnostics,
        limit=(
            MAX_GITHUB_ANNOTATIONS
            if len(report.diagnostics) <= MAX_GITHUB_ANNOTATIONS
            else MAX_GITHUB_ANNOTATIONS - 1
        ),
    )
    lines = [_github_annotation(diagnostic) for diagnostic in diagnostics]
    omitted = len(report.diagnostics) - len(diagnostics)
    if omitted:
        title = _github_property("YAGA size policy", maximum=MAX_GITHUB_TITLE)
        data = _github_data(
            f"{omitted} additional committed blob-size violation(s) omitted",
            maximum=MAX_DIAGNOSTIC_MESSAGE,
        )
        lines.append(f"::error title={title}::{data}")
    lines.append(_summary(report, prefix="YAGA size policy"))
    return "\n".join(lines)


def _github_annotation(diagnostic: SizeDiagnostic) -> str:
    title = _github_property(f"YAGA {diagnostic.code}", maximum=MAX_GITHUB_TITLE)
    message = safe_text(diagnostic.message, maximum=MAX_DIAGNOSTIC_MESSAGE)
    data = _github_data(
        f"{message}; {diagnostic.size} > {diagnostic.limit} bytes"
        + (
            f"; first matching pattern: "
            f"{safe_text(diagnostic.pattern, maximum=MAX_DISPLAY_PATTERN)}"
            if diagnostic.pattern is not None
            else ""
        ),
        maximum=MAX_DIAGNOSTIC_MESSAGE,
    )
    if diagnostic.code == SIZE_TOTAL_CODE:
        return f"::error title={title}::{data}"
    if diagnostic.code != SIZE_BLOB_CODE or diagnostic.path is None:
        raise AssertionError("size report has an invalid diagnostic")
    file_path = _github_property(diagnostic.path, maximum=MAX_DISPLAY_PATH)
    return f"::error file={file_path},title={title}::{data}"


def _visible_with_aggregate(
    values: tuple[SizeDiagnostic, ...],
    *,
    limit: int,
) -> tuple[SizeDiagnostic, ...]:
    if len(values) <= limit:
        return values
    aggregate = values[-1] if values and values[-1].code == SIZE_TOTAL_CODE else None
    if aggregate is None:
        return values[:limit]
    return (*values[: max(0, limit - 1)], aggregate)


def _summary(report: SizeReport, *, prefix: str = "Size policy") -> str:
    total = "exceeded" if report.total_exceeded else "not exceeded"
    if report.policy.max_total_blob_bytes is None:
        total = "not configured"
    return (
        f"{prefix}: {len(report.selection.blobs)} blob(s), "
        f"{len(report.selection.gitlinks)} gitlink(s); {report.total_bytes} byte(s); "
        f"{len(report.oversized_blobs)} oversized blob(s); total limit {total}."
    )


def _validate_report(report: SizeReport) -> None:
    diagnostics = tuple(report.diagnostics)
    if any(item.code not in _SIZE_CODES for item in diagnostics):
        raise AssertionError("size report has an unknown diagnostic code")
    saw_total = False
    blob_paths: list[str] = []
    for diagnostic in diagnostics:
        if diagnostic.code == SIZE_TOTAL_CODE:
            if saw_total or diagnostic.path is not None or diagnostic.pattern is not None:
                raise AssertionError("size report has an invalid aggregate diagnostic")
            saw_total = True
            continue
        if saw_total or diagnostic.path is None:
            raise AssertionError("size report has an invalid per-blob diagnostic")
        blob_paths.append(diagnostic.path)
    if tuple(blob_paths) != tuple(sorted(blob_paths)) or len(set(blob_paths)) != len(blob_paths):
        raise AssertionError("size report blob diagnostics are not lexical and unique")
    if len(blob_paths) != len(report.oversized_blobs):
        raise AssertionError("size report oversized-blob summary is inconsistent")
    if saw_total != report.total_exceeded:
        raise AssertionError("size report total summary is inconsistent")
    expected_status = "passed" if not diagnostics else "failed"
    if report.status != expected_status or report.valid != (not diagnostics):
        raise AssertionError("size report status is inconsistent")


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
