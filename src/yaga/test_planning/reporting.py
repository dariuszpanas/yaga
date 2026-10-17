"""Versioned canonical plan documents and bounded human-readable summaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from enum import StrEnum
from typing import Any

from yaga.errors import ConfigurationError, YagaError, safe_error_text
from yaga.test_planning.limits import MAX_PLAN_BYTES
from yaga.test_planning.models import LoadedTestPolicy
from yaga.test_planning.reports import PlannedTests

MAX_DISPLAY_REASONS = 256
MAX_DISPLAY_VALUE = 200
MAX_ERROR_MESSAGE = 1_000
TEST_PLAN_VERSION = 1


class TestOutputFormat(StrEnum):
    """The installed planner's supported output formats."""

    TEXT = "text"
    JSON = "json"


def test_plan_document(planned: PlannedTests) -> dict[str, Any]:
    """Return complete canonical semantics and their digest within the final byte bound."""
    selection = planned.selection
    plan = {
        "test-plan-version": TEST_PLAN_VERSION,
        "policy_sha256": planned.policy_sha256,
        "bindings_sha256": planned.bindings_sha256,
        "profile": selection.profile,
        "requested_mode": selection.requested_mode.value,
        "effective_mode": selection.effective_mode.value,
        "source": asdict(planned.source),
        "changed_paths": list(selection.changed_paths)
        if selection.changed_paths is not None
        else None,
        "inventory": [
            {
                "id": suite.id,
                "binding": suite.binding,
                "tags": list(suite.tags),
                "variants": list(suite.variants),
                "needs": list(suite.needs),
                "requires": list(suite.requires),
            }
            for suite in selection.inventory
        ],
        "selected": [
            {"suite": item.suite, "variants": list(item.variants)} for item in selection.selected
        ],
        "reasons": [asdict(reason) for reason in selection.reasons],
    }
    semantic_json = _bounded_json(plan)
    document = {
        "test-plan-version": TEST_PLAN_VERSION,
        "plan_digest": hashlib.sha256(semantic_json.encode("utf-8")).hexdigest(),
        "plan": plan,
    }
    _bounded_json(document)
    return document


def render_test_plan(planned: PlannedTests, output_format: TestOutputFormat) -> str:
    """Render a plan, never a claim that its selected tests ran or passed."""
    document = test_plan_document(planned)
    if output_format is TestOutputFormat.JSON:
        return _bounded_json(document)
    selection = planned.selection
    lines = [
        f"Test plan: {selection.profile}",
        f"Mode: {selection.requested_mode.value} requested; {selection.effective_mode.value} effective",
        f"Execution commit: {planned.source.execution_commit}",
        f"Execution tree: {planned.source.execution_tree}",
        f"Policy SHA-256: {planned.policy_sha256}",
        f"Bindings SHA-256: {planned.bindings_sha256 or 'none (preview only)'}",
        f"Plan digest: {document['plan_digest']}",
    ]
    if selection.changed_paths is None:
        lines.append("Changed paths: unavailable for a revision-only plan")
    else:
        lines.append(f"Changed paths: {len(selection.changed_paths)}")
        lines.append(f"Comparison commit: {planned.source.comparison_commit}")
    cells = sum(len(item.variants) for item in selection.selected)
    lines.append(
        f"Selected: {len(selection.selected)}/{len(selection.inventory)} suite(s), {cells} variant cell(s)"
    )
    for item in selection.selected:
        lines.append(f"  {item.suite}: {', '.join(item.variants)}")
    lines.append(f"Reasons: {len(selection.reasons)}")
    for reason in selection.reasons[:MAX_DISPLAY_REASONS]:
        details = (
            f"{key}={safe_error_text(value, maximum=MAX_DISPLAY_VALUE)}"
            for key, value in asdict(reason).items()
            if key != "code" and value is not None
        )
        lines.append(f"  [{reason.code}] {'; '.join(details)}".rstrip())
    omitted = len(selection.reasons) - MAX_DISPLAY_REASONS
    if omitted > 0:
        lines.append(f"  {omitted} additional reason(s) omitted from this display")
    lines.append("Planning only; no tests were executed.")
    return "\n".join(lines)


def render_test_policy(loaded: LoadedTestPolicy, output_format: TestOutputFormat) -> str:
    """Render successful policy validation without local-path or execution claims."""
    policy = loaded.policy
    cells = sum(len(suite.variants) for suite in policy.suites)
    profiles = sorted(profile.name for profile in policy.profiles)
    document = {
        "schema_version": 1,
        "kind": "test_policy",
        "status": "valid",
        "valid": True,
        "test-policy-version": policy.test_policy_version,
        "policy_sha256": loaded.sha256,
        "suites": len(policy.suites),
        "cells": cells,
        "profiles": profiles,
        "rules": len(policy.rules),
    }
    if output_format is TestOutputFormat.JSON:
        return _bounded_json(document)
    return "\n".join(
        (
            "Test policy is valid (version 1).",
            f"Policy SHA-256: {loaded.sha256}",
            f"Inventory: {len(policy.suites)} suite(s), {cells} variant cell(s)",
            f"Profiles: {', '.join(profiles)}",
            f"Rules: {len(policy.rules)}",
            "Policy validation only; no tests were executed.",
        )
    )


def render_test_error(
    error: YagaError, output_format: TestOutputFormat, *, kind: str = "test_plan"
) -> str:
    """Render operational errors through the existing independent error envelope."""
    if kind not in {"test_plan", "test_policy"}:
        raise ValueError("test error kind must be test_plan or test_policy")
    message = safe_error_text(error, maximum=MAX_ERROR_MESSAGE)
    error_kind = safe_error_text(error.kind, maximum=64)
    if output_format is TestOutputFormat.JSON:
        return _bounded_json(
            {
                "schema_version": 1,
                "kind": kind,
                "status": "error",
                "valid": False,
                "error": {"kind": error_kind, "message": message},
            }
        )
    return f"YAGA {error_kind} error: {message}"


def _bounded_json(document: dict[str, Any]) -> str:
    """Bound UTF-8 bytes while encoding, including the final document envelope."""
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    remaining = MAX_PLAN_BYTES
    chunks = []
    for chunk in encoder.iterencode(document):
        remaining -= len(chunk.encode("utf-8"))
        if remaining < 0:
            raise ConfigurationError(f"test report exceeds the hard {MAX_PLAN_BYTES}-byte limit")
        chunks.append(chunk)
    return "".join(chunks)
