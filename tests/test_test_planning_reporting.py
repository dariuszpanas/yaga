"""Canonical source-bound plan output, digests, and bounded presentation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import cast

import pytest

from yaga.errors import ConfigurationError, GitError, InputError
from yaga.test_planning import reporting
from yaga.test_planning.models import (
    LoadedTestPolicy,
    Profile,
    QualificationPolicy,
    SelectionMode,
    SelectionRule,
    Suite,
)
from yaga.test_planning.planner import plan_tests
from yaga.test_planning.policy import load_test_policy
from yaga.test_planning.reporting import TestOutputFormat as OutputFormat
from yaga.test_planning.reporting import render_test_error, render_test_plan, render_test_policy
from yaga.test_planning.reporting import test_plan_document as plan_document
from yaga.test_planning.reports import PlannedTests, validate_bindings_sha256
from yaga.test_planning.selection import SelectedSuite, SelectionReason
from yaga.test_planning.source import PlanSource

POLICY_SHA = "a" * 64
BINDINGS_SHA = "b" * 64
SOURCE = PlanSource("1" * 40, "2" * 40, "3" * 40, "2" * 40, "4" * 40)
REVISION_SOURCE = PlanSource(None, None, None, "2" * 40, "4" * 40)


@pytest.fixture
def policy() -> QualificationPolicy:
    return QualificationPolicy(
        1,
        suites=(
            Suite("backend", "pytest", tags=("python",), needs=("image",)),
            Suite("image", "docker", variants=("arm", "amd"), requires=("docker",)),
            Suite("frontend", "vitest"),
        ),
        profiles=(Profile("dev", SelectionMode.SELECTIVE), Profile("full", SelectionMode.FULL)),
        rules=(SelectionRule("backend", ("src/**",), select=("backend",)),),
        no_impact=("docs/**",),
    )


@pytest.fixture
def planned(policy: QualificationPolicy) -> PlannedTests:
    return PlannedTests(
        POLICY_SHA,
        BINDINGS_SHA,
        SOURCE,
        plan_tests(policy, profile="dev", changed_paths=("src/worker.py",)),
    )


def test_exact_versioned_envelope_and_complete_semantics(planned: PlannedTests) -> None:
    document = plan_document(planned)
    assert set(document) == {"test-plan-version", "plan_digest", "plan"}
    assert document["test-plan-version"] == 1
    plan = document["plan"]
    assert set(plan) == {
        "test-plan-version",
        "policy_sha256",
        "bindings_sha256",
        "profile",
        "requested_mode",
        "effective_mode",
        "source",
        "changed_paths",
        "inventory",
        "selected",
        "reasons",
    }
    assert plan["test-plan-version"] == 1
    assert plan["policy_sha256"] == POLICY_SHA
    assert plan["bindings_sha256"] == BINDINGS_SHA
    assert plan["profile"] == "dev"
    assert plan["requested_mode"] == plan["effective_mode"] == "selective"
    assert plan["source"] == {
        "base_commit": "1" * 40,
        "head_commit": "2" * 40,
        "comparison_commit": "3" * 40,
        "execution_commit": "2" * 40,
        "execution_tree": "4" * 40,
    }
    assert plan["changed_paths"] == ["src/worker.py"]
    assert [suite["id"] for suite in plan["inventory"]] == ["backend", "frontend", "image"]
    assert plan["inventory"][2] == {
        "id": "image",
        "binding": "docker",
        "tags": [],
        "variants": ["amd", "arm"],
        "needs": [],
        "requires": ["docker"],
    }
    assert plan["selected"] == [
        {"suite": "image", "variants": ["amd", "arm"]},
        {"suite": "backend", "variants": ["default"]},
    ]
    assert plan["reasons"] == [
        {
            "code": "test.prerequisite",
            "suite": "image",
            "rule": None,
            "path": None,
            "required_by": "backend",
        },
        {
            "code": "test.rule",
            "suite": "backend",
            "rule": "backend",
            "path": "src/worker.py",
            "required_by": None,
        },
    ]
    assert json.loads(render_test_plan(planned, OutputFormat.JSON)) == document


def test_digest_hashes_only_complete_canonical_semantic_plan(planned: PlannedTests) -> None:
    document = plan_document(planned)
    canonical = json.dumps(
        document["plan"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert document["plan_digest"] == hashlib.sha256(canonical).hexdigest()
    assert document["plan_digest"] in render_test_plan(planned, OutputFormat.TEXT)
    assert document == plan_document(planned)
    assert "\n" not in render_test_plan(planned, OutputFormat.JSON)


def test_normalized_input_orders_have_one_digest(policy: QualificationPolicy) -> None:
    original = PlannedTests(
        POLICY_SHA,
        None,
        SOURCE,
        plan_tests(policy, profile="dev", changed_paths=("src/b.py", "src/a.py")),
    )
    reordered = PlannedTests(
        POLICY_SHA,
        None,
        SOURCE,
        plan_tests(
            replace(policy, suites=tuple(reversed(policy.suites))),
            profile="dev",
            changed_paths=("src/a.py", "src/b.py"),
        ),
    )
    assert plan_document(original) == plan_document(reordered)


def test_policy_comments_change_digest_but_local_paths_do_not(tmp_path: Path) -> None:
    raw = b"""test-policy-version = 1
[[suites]]
id = "unit"
binding = "pytest"
[profiles.full]
mode = "full"
"""
    paths = [tmp_path / name for name in ("one.toml", "two.toml", "comment.toml")]
    for path, content in zip(paths, (raw, raw, raw + b"# comment\n"), strict=True):
        path.write_bytes(content)
    loaded = [load_test_policy(path) for path in paths]
    assert loaded[0].policy == loaded[1].policy == loaded[2].policy
    documents = [
        plan_document(
            PlannedTests(
                item.sha256,
                None,
                REVISION_SOURCE,
                plan_tests(item.policy, profile="full", changed_paths=None),
            )
        )
        for item in loaded
    ]
    assert documents[0] == documents[1]
    assert documents[0]["plan_digest"] != documents[2]["plan_digest"]
    assert str(tmp_path) not in json.dumps(documents)


@pytest.mark.parametrize(
    "changes",
    [
        {"bindings_sha256": "c" * 64},
        {"bindings_sha256": None},
        {"policy_sha256": "c" * 64},
        {"source": replace(SOURCE, base_commit="5" * 40)},
        {"source": replace(SOURCE, comparison_commit="5" * 40)},
        {"source": replace(SOURCE, head_commit="5" * 40, execution_commit="5" * 40)},
        {"source": replace(SOURCE, execution_tree="5" * 40)},
    ],
)
def test_identity_changes_invalidate_digest(
    planned: PlannedTests, changes: dict[str, object]
) -> None:
    changed = replace(planned, **changes)
    assert plan_document(changed)["plan_digest"] != plan_document(planned)["plan_digest"]


def test_revision_only_nulls_and_preview_are_explicit(policy: QualificationPolicy) -> None:
    planned = PlannedTests(
        POLICY_SHA,
        None,
        REVISION_SOURCE,
        plan_tests(policy, profile="full", changed_paths=None),
    )
    plan = plan_document(planned)["plan"]
    assert plan["changed_paths"] is None
    assert plan["bindings_sha256"] is None
    assert all(
        plan["source"][key] is None for key in ("base_commit", "head_commit", "comparison_commit")
    )
    assert plan["reasons"] == [
        {
            "code": "test.profile-full",
            "suite": None,
            "rule": None,
            "path": None,
            "required_by": None,
        }
    ]
    text = render_test_plan(planned, OutputFormat.TEXT)
    assert "preview only" in text
    assert "no tests were executed" in text
    assert "passed" not in text


def test_complete_empty_change_is_distinct_from_revision_only(policy: QualificationPolicy) -> None:
    planned = PlannedTests(
        POLICY_SHA,
        None,
        SOURCE,
        plan_tests(policy, profile="dev", changed_paths=()),
    )
    plan = plan_document(planned)["plan"]
    assert plan["changed_paths"] == []
    assert plan["selected"] == []
    assert plan["reasons"] == []


def test_unicode_json_is_exact_while_text_is_bounded(policy: QualificationPolicy) -> None:
    path = "src/日本\u00a0語" + "a" * 400
    planned = PlannedTests(
        POLICY_SHA,
        None,
        SOURCE,
        plan_tests(policy, profile="dev", changed_paths=(path,)),
    )
    output = render_test_plan(planned, OutputFormat.JSON)
    assert path in output
    assert "\\u65e5" not in output
    assert json.loads(output)["plan"]["reasons"][-1]["path"] == path
    text = render_test_plan(planned, OutputFormat.TEXT)
    assert path not in text
    assert "日本 語" in text
    assert "…" in text


def test_text_reason_omission_is_exact_and_json_keeps_every_reason(
    policy: QualificationPolicy,
) -> None:
    planned = PlannedTests(
        POLICY_SHA,
        None,
        SOURCE,
        plan_tests(
            policy, profile="dev", changed_paths=tuple(f"src/{index}" for index in range(300))
        ),
    )
    text = render_test_plan(planned, OutputFormat.TEXT)
    assert sum(line.startswith("  [test.") for line in text.splitlines()) == 256
    assert "45 additional reason(s) omitted" in text
    assert len(plan_document(planned)["plan"]["reasons"]) == 301


def test_byte_limit_includes_envelope_and_accepts_exact_boundary(
    planned: PlannedTests, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = render_test_plan(planned, OutputFormat.JSON)
    size = len(output.encode("utf-8"))
    semantic_size = len(
        json.dumps(plan_document(planned)["plan"], separators=(",", ":")).encode("utf-8")
    )
    assert semantic_size < size - 1
    monkeypatch.setattr(reporting, "MAX_PLAN_BYTES", size)
    assert render_test_plan(planned, OutputFormat.JSON) == output
    monkeypatch.setattr(reporting, "MAX_PLAN_BYTES", size - 1)
    with pytest.raises(ConfigurationError, match="test report exceeds"):
        plan_document(planned)
    with pytest.raises(ConfigurationError, match="test report exceeds"):
        render_test_plan(planned, OutputFormat.TEXT)


def test_final_bound_counts_utf8_bytes(
    policy: QualificationPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    planned = PlannedTests(
        POLICY_SHA,
        None,
        SOURCE,
        plan_tests(policy, profile="dev", changed_paths=("src/日本語",)),
    )
    output = render_test_plan(planned, OutputFormat.JSON)
    assert len(output) < len(output.encode("utf-8"))
    monkeypatch.setattr(reporting, "MAX_PLAN_BYTES", len(output))
    with pytest.raises(ConfigurationError, match="test report exceeds"):
        render_test_plan(planned, OutputFormat.JSON)


def test_policy_check_success_contract(policy: QualificationPolicy, tmp_path: Path) -> None:
    loaded = LoadedTestPolicy(policy, tmp_path / "policy.toml", POLICY_SHA)
    assert json.loads(render_test_policy(loaded, OutputFormat.JSON)) == {
        "schema_version": 1,
        "kind": "test_policy",
        "status": "valid",
        "valid": True,
        "test-policy-version": 1,
        "policy_sha256": POLICY_SHA,
        "suites": 3,
        "cells": 4,
        "profiles": ["dev", "full"],
        "rules": 1,
    }
    text = render_test_policy(loaded, OutputFormat.TEXT)
    assert "valid (version 1)" in text
    assert "no tests were executed" in text
    assert "passed" not in text
    assert str(tmp_path) not in text


@pytest.mark.parametrize("kind", ["test_plan", "test_policy"])
@pytest.mark.parametrize("error_type", [ConfigurationError, GitError, InputError])
def test_errors_are_sanitized_bounded_and_not_success_shaped(
    kind: str, error_type: type[ConfigurationError | GitError | InputError]
) -> None:
    error = error_type("bad\x1b[31m\n\u202e" + "x" * 2000)
    document = json.loads(render_test_error(error, OutputFormat.JSON, kind=kind))
    assert set(document) == {"schema_version", "kind", "status", "valid", "error"}
    assert document["schema_version"] == 1
    assert document["kind"] == kind
    assert document["status"] == "error"
    assert document["valid"] is False
    assert document["error"]["kind"] == error.kind
    assert len(document["error"]["message"]) <= 1000
    text = render_test_error(error, OutputFormat.TEXT, kind=kind)
    assert "\x1b" not in text and "\u202e" not in text and "\n" not in text
    assert len(text) < 1100


def test_error_kind_is_closed() -> None:
    with pytest.raises(ValueError, match="test error kind"):
        render_test_error(InputError("bad"), OutputFormat.JSON, kind="other")


@pytest.mark.parametrize(
    "value", ["", "A" * 64, "a" * 63, "a" * 65, "g" * 64, True, 1, [], "a" * 64 + "\n"]
)
def test_bindings_digest_validation_is_strict(value: object) -> None:
    with pytest.raises(InputError, match="bindings-sha256"):
        validate_bindings_sha256(cast(str, value))


def test_bindings_digest_is_optional() -> None:
    assert validate_bindings_sha256(None) is None
    assert validate_bindings_sha256(BINDINGS_SHA) == BINDINGS_SHA


def test_source_and_selection_shapes_cannot_be_mixed(planned: PlannedTests) -> None:
    with pytest.raises(ValueError, match="revision-only source"):
        replace(planned, source=REVISION_SOURCE)
    with pytest.raises(ValueError, match="range source"):
        replace(planned, selection=replace(planned.selection, changed_paths=None))
    with pytest.raises(ValueError, match="policy_sha256"):
        replace(planned, policy_sha256="a" * 40)
    with pytest.raises(FrozenInstanceError):
        setattr(planned, "policy_sha256", "c" * 64)  # noqa: B010


def test_incomplete_variants_and_noncanonical_selection_are_rejected(planned: PlannedTests) -> None:
    with pytest.raises(ValueError, match="all variants"):
        replace(
            planned,
            selection=replace(
                planned.selection,
                selected=(SelectedSuite("image", ("amd",)), planned.selection.selected[1]),
            ),
        )
    with pytest.raises(ValueError, match="prerequisite order"):
        replace(
            planned,
            selection=replace(
                planned.selection, selected=tuple(reversed(planned.selection.selected))
            ),
        )
    with pytest.raises(ValueError, match="canonically ordered"):
        replace(
            planned,
            selection=replace(
                planned.selection, reasons=tuple(reversed(planned.selection.reasons))
            ),
        )


def test_noncanonical_inventory_is_rejected(planned: PlannedTests) -> None:
    with pytest.raises(ValueError, match="canonical suite order"):
        replace(
            planned,
            selection=replace(
                planned.selection, inventory=tuple(reversed(planned.selection.inventory))
            ),
        )
    inventory = planned.selection.inventory
    with pytest.raises(ValueError, match="canonical metadata order"):
        replace(
            planned,
            selection=replace(
                planned.selection,
                inventory=(*inventory[:-1], replace(inventory[-1], variants=("arm", "amd"))),
            ),
        )


def test_full_selection_requires_complete_inventory_and_profile_reason(
    policy: QualificationPolicy,
) -> None:
    planned = PlannedTests(
        POLICY_SHA,
        None,
        REVISION_SOURCE,
        plan_tests(policy, profile="full", changed_paths=None),
    )
    with pytest.raises(ValueError, match="entire inventory"):
        replace(planned, selection=replace(planned.selection, selected=()))
    with pytest.raises(ValueError, match="profile-full reason"):
        replace(planned, selection=replace(planned.selection, reasons=()))
    with pytest.raises(ValueError, match="full test plan"):
        replace(
            planned,
            selection=replace(planned.selection, effective_mode=SelectionMode.SELECTIVE),
        )


def test_fallback_selection_keeps_exact_cause(policy: QualificationPolicy) -> None:
    planned = PlannedTests(
        POLICY_SHA,
        None,
        SOURCE,
        plan_tests(policy, profile="dev", changed_paths=("unmapped",)),
    )
    plan = plan_document(planned)["plan"]
    assert plan["requested_mode"] == "selective"
    assert plan["effective_mode"] == "full"
    assert plan["reasons"] == [
        {
            "code": "test.unknown-path",
            "suite": None,
            "rule": None,
            "path": "unmapped",
            "required_by": None,
        }
    ]
    with pytest.raises(ValueError, match="fallback reasons"):
        replace(planned, selection=replace(planned.selection, reasons=()))


def test_revision_only_selective_profile_is_rejected(policy: QualificationPolicy) -> None:
    selection = plan_tests(policy, profile="dev", changed_paths=())
    with pytest.raises(ValueError, match="require a full profile"):
        PlannedTests(POLICY_SHA, None, REVISION_SOURCE, replace(selection, changed_paths=None))


@pytest.mark.parametrize(
    "reason",
    [
        SelectionReason("test.unknown"),
        SelectionReason("test.always"),
        SelectionReason("test.rule", suite="backend", rule="bad!", path="src/worker.py"),
        SelectionReason("test.rule", suite="frontend", rule="good", path="src/worker.py"),
        SelectionReason("test.rule", suite="backend", rule="good", path="src/other.py"),
        SelectionReason("test.prerequisite", suite="backend", required_by="image"),
    ],
)
def test_inconsistent_reasons_are_rejected(planned: PlannedTests, reason: SelectionReason) -> None:
    with pytest.raises(ValueError, match="reason"):
        replace(planned, selection=replace(planned.selection, reasons=(reason,)))
