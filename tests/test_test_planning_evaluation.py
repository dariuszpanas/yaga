"""Exact obligation/result correlation, fail-closed identity, and bounded reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, cast

import pytest

from yaga.errors import ConfigurationError, InputError
from yaga.test_planning import evaluation
from yaga.test_planning.evaluation import (
    EvaluationCounts,
    EvaluationDiagnostic,
    encode_evaluation,
    evaluate_results,
    evaluation_document,
)
from yaga.test_planning.models import (
    Profile,
    QualificationPolicy,
    SelectionMode,
    SelectionRule,
    Suite,
)
from yaga.test_planning.planner import plan_tests
from yaga.test_planning.policy import load_test_policy
from yaga.test_planning.reporting import test_plan_document as plan_document
from yaga.test_planning.reports import PlannedTests
from yaga.test_planning.results import (
    CellResult,
    ResultOutcome,
    encode_results,
)
from yaga.test_planning.results import (
    TestResults as Results,
)
from yaga.test_planning.source import PlanSource

POLICY_SHA = "a" * 64
BINDINGS_SHA = "b" * 64
SOURCE = PlanSource("1" * 40, "2" * 40, "3" * 40, "2" * 40, "4" * 40)
EXPECTED: dict[str, Any] = {
    "expected_policy_sha256": POLICY_SHA,
    "expected_bindings_sha256": BINDINGS_SHA,
    "expected_execution_commit": SOURCE.execution_commit,
    "expected_execution_tree": SOURCE.execution_tree,
    "expected_run_id": "run:123",
    "expected_attempt": 1,
}


@pytest.fixture
def policy() -> QualificationPolicy:
    return QualificationPolicy(
        1,
        suites=(
            Suite("backend", "pytest", needs=("image",)),
            Suite("frontend", "vitest"),
            Suite("image", "docker", variants=("amd", "arm")),
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
        plan_tests(policy, profile="dev", changed_paths=("src/main.py",)),
    )


def _results(planned: PlannedTests, outcome: ResultOutcome = ResultOutcome.PASSED) -> Results:
    catalog = {suite.id: suite for suite in planned.selection.inventory}
    return Results(
        plan_digest=plan_document(planned)["plan_digest"],
        policy_sha256=planned.policy_sha256,
        bindings_sha256=cast(str, planned.bindings_sha256),
        execution_commit=planned.source.execution_commit,
        execution_tree=planned.source.execution_tree,
        run_id="run:123",
        attempt=1,
        results=tuple(
            CellResult(item.suite, variant, catalog[item.suite].binding, outcome)
            for item in planned.selection.selected
            for variant in item.variants
        ),
    )


def test_complete_passed_report_retains_source_and_canonical_result_digest(
    planned: PlannedTests,
) -> None:
    receipts = _results(planned)
    report = evaluate_results(planned, receipts, **EXPECTED)
    document = evaluation_document(report)
    assert set(document) == {
        "test-evaluation-version",
        "plan_digest",
        "policy_sha256",
        "bindings_sha256",
        "results_sha256",
        "source",
        "run_id",
        "attempt",
        "status",
        "complete",
        "passed",
        "exit_code",
        "counts",
        "diagnostic_count",
        "omitted_diagnostics",
        "diagnostics",
    }
    assert document["test-evaluation-version"] == 1
    assert report.status == "passed" and report.complete and report.passed
    assert report.exit_code == 0
    assert document["source"] == plan_document(planned)["plan"]["source"]
    assert report.plan_digest == receipts.plan_digest
    assert report.policy_sha256 == POLICY_SHA and report.bindings_sha256 == BINDINGS_SHA
    assert report.results_sha256 == hashlib.sha256(encode_results(receipts)[:-1]).hexdigest()
    assert document["counts"] == {
        "selected_cells": 3,
        "received_cells": 3,
        "passed": 3,
        "failed": 0,
        "pending": 0,
        "cancelled": 0,
        "blocked": 0,
        "missing": 0,
    }
    assert report.diagnostics == () and report.diagnostic_count == report.omitted_diagnostics == 0
    assert json.loads(encode_evaluation(report)) == document
    assert encode_evaluation(report).endswith(b"\n")


@pytest.mark.parametrize(
    ("outcome", "status", "complete", "code"),
    [
        (ResultOutcome.FAILED, "failed", True, "test.failed"),
        (ResultOutcome.CANCELLED, "failed", True, "test.cancelled"),
        (ResultOutcome.PENDING, "incomplete", False, "test.required-result"),
        (ResultOutcome.BLOCKED, "incomplete", False, "test.required-result"),
    ],
)
def test_every_nonpassed_outcome_blocks_success(
    planned: PlannedTests, outcome: ResultOutcome, status: str, complete: bool, code: str
) -> None:
    receipts = _results(planned)
    receipts = replace(
        receipts, results=(replace(receipts.results[0], outcome=outcome), *receipts.results[1:])
    )
    report = evaluate_results(planned, receipts, **EXPECTED)
    assert report.status == status and report.complete is complete
    assert not report.passed and report.exit_code == 1
    assert len(report.diagnostics) == 1 and report.diagnostic_count == 1
    assert report.diagnostics[0].code == code
    assert report.diagnostics[0].outcome is outcome
    assert getattr(report.counts, outcome.value) == 1
    assert report.counts.passed == 2


def test_missing_prerequisite_cannot_be_satisfied_by_dependent_pass(planned: PlannedTests) -> None:
    receipts = _results(planned)
    receipts = replace(
        receipts, results=tuple(item for item in receipts.results if item.suite == "backend")
    )
    report = evaluate_results(planned, receipts, **EXPECTED)
    assert report.status == "incomplete" and not report.complete and not report.passed
    assert report.counts.missing == 2 and report.counts.passed == 1
    assert [
        (item.suite, item.variant, item.binding, item.code, item.outcome)
        for item in report.diagnostics
    ] == [
        ("image", "amd", "docker", "test.required-result", None),
        ("image", "arm", "docker", "test.required-result", None),
    ]


def test_failed_has_precedence_over_missing_but_completeness_remains_false(
    planned: PlannedTests,
) -> None:
    receipts = _results(planned)
    receipts = replace(
        receipts, results=(replace(receipts.results[0], outcome=ResultOutcome.FAILED),)
    )
    report = evaluate_results(planned, receipts, **EXPECTED)
    assert report.status == "failed" and not report.complete
    assert report.counts.failed == 1 and report.counts.missing == 2


def test_empty_plan_is_explicit_zero_obligations_without_claiming_execution(
    policy: QualificationPolicy,
) -> None:
    planned = PlannedTests(
        POLICY_SHA, BINDINGS_SHA, SOURCE, plan_tests(policy, profile="dev", changed_paths=())
    )
    report = evaluate_results(planned, _results(planned), **EXPECTED)
    assert report.passed and report.complete and report.exit_code == 0
    assert report.counts == EvaluationCounts(0, 0, 0, 0, 0, 0, 0, 0)
    assert "executed" not in evaluation_document(report)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plan_digest", "c" * 64),
        ("policy_sha256", "c" * 64),
        ("bindings_sha256", "c" * 64),
        ("execution_commit", "5" * 40),
        ("execution_tree", "5" * 40),
        ("run_id", "different-run"),
        ("attempt", 2),
    ],
)
def test_stale_or_mismatched_receipt_identity_is_input_error(
    planned: PlannedTests, field: str, value: object
) -> None:
    with pytest.raises(InputError):
        evaluate_results(planned, replace(_results(planned), **{field: value}), **EXPECTED)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_policy_sha256", "c" * 64),
        ("expected_policy_sha256", True),
        ("expected_bindings_sha256", "c" * 64),
        ("expected_bindings_sha256", None),
        ("expected_execution_commit", "5" * 40),
        ("expected_execution_commit", True),
        ("expected_execution_tree", "5" * 40),
        ("expected_execution_tree", "F" * 40),
        ("expected_run_id", "another"),
        ("expected_run_id", "bad\nrun"),
        ("expected_attempt", 2),
        ("expected_attempt", True),
        ("expected_attempt", 0),
    ],
)
def test_independent_expected_values_are_required_and_strict(
    planned: PlannedTests, field: str, value: object
) -> None:
    expected: dict[str, Any] = {**EXPECTED, field: value}
    with pytest.raises(InputError):
        evaluate_results(planned, _results(planned), **expected)


def test_plan_identity_is_independently_checked_even_if_receipts_follow_tampered_plan(
    planned: PlannedTests,
) -> None:
    for changed in (
        replace(planned, policy_sha256="c" * 64),
        replace(planned, bindings_sha256="c" * 64),
        replace(planned, source=replace(planned.source, execution_tree="5" * 40)),
    ):
        with pytest.raises(InputError):
            evaluate_results(changed, _results(changed), **EXPECTED)


def test_preview_plan_cannot_qualify_with_a_claimed_manifest(planned: PlannedTests) -> None:
    receipts = _results(planned)
    preview = replace(planned, bindings_sha256=None)
    receipts = replace(receipts, plan_digest=plan_document(preview)["plan_digest"])
    with pytest.raises(InputError, match="preview"):
        evaluate_results(preview, receipts, **EXPECTED)


@pytest.mark.parametrize(
    "cell",
    [
        CellResult("unknown", "default", "unknown", ResultOutcome.PASSED),
        CellResult("frontend", "default", "vitest", ResultOutcome.PASSED),
        CellResult("image", "unknown", "docker", ResultOutcome.PASSED),
        CellResult("image", "amd", "wrong-binding", ResultOutcome.PASSED),
    ],
)
def test_unknown_unselected_and_wrongly_bound_cells_are_input_errors(
    planned: PlannedTests, cell: CellResult
) -> None:
    receipts = replace(_results(planned), results=(cell,))
    with pytest.raises(InputError, match="cell"):
        evaluate_results(planned, receipts, **EXPECTED)


def test_cell_arrival_order_does_not_change_result_digest_or_evaluation(
    planned: PlannedTests,
) -> None:
    receipts = _results(planned, ResultOutcome.FAILED)
    first = evaluate_results(planned, receipts, **EXPECTED)
    second = evaluate_results(
        planned, replace(receipts, results=tuple(reversed(receipts.results))), **EXPECTED
    )
    assert encode_evaluation(first) == encode_evaluation(second)


def test_summary_is_retained_in_fingerprint_but_not_interpolated_into_diagnostics(
    planned: PlannedTests,
) -> None:
    receipts = _results(planned, ResultOutcome.FAILED)
    text = "\x1b[2J\nFAKE SUCCESS\u202e"
    hostile = replace(
        receipts, results=(replace(receipts.results[0], summary=text), *receipts.results[1:])
    )
    report = evaluate_results(planned, hostile, **EXPECTED)
    assert report.results_sha256 != evaluate_results(planned, receipts, **EXPECTED).results_sha256
    assert all(item.message == "Required cell failed." for item in report.diagnostics)
    assert b"FAKE SUCCESS" not in encode_evaluation(report)
    assert b"\\u001b" in encode_results(hostile)


@pytest.mark.parametrize("filename", ["polyglot.toml", "django-ray.toml"])
def test_actual_examples_require_every_selected_cell_and_prerequisite(filename: str) -> None:
    loaded = load_test_policy(
        Path(__file__).resolve().parents[1] / "docs/designs/examples/test-planning" / filename
    )
    for profile in loaded.policy.profiles:
        paths = (
            None if profile.mode is SelectionMode.FULL else ("qualification/docker/scenario.py",)
        )
        source = (
            PlanSource(None, None, None, SOURCE.execution_commit, SOURCE.execution_tree)
            if paths is None
            else SOURCE
        )
        planned = PlannedTests(
            loaded.sha256,
            BINDINGS_SHA,
            source,
            plan_tests(loaded.policy, profile=profile.name, changed_paths=paths),
        )
        expected: dict[str, Any] = {**EXPECTED, "expected_policy_sha256": loaded.sha256}
        receipts = _results(planned)
        report = evaluate_results(planned, receipts, **expected)
        assert report.passed
        assert report.counts.selected_cells == sum(
            len(item.variants) for item in planned.selection.selected
        )
        for index, cell in enumerate(receipts.results):
            missing = replace(
                receipts, results=receipts.results[:index] + receipts.results[index + 1 :]
            )
            report = evaluate_results(planned, missing, **expected)
            assert report.exit_code == 1 and report.counts.missing == 1
            assert report.diagnostics[0].suite == cell.suite
            assert report.diagnostics[0].variant == cell.variant


def test_all_512_obligations_are_counted_even_when_diagnostics_are_capped() -> None:
    policy = QualificationPolicy(
        1,
        suites=tuple(
            Suite(
                f"suite{index:03d}",
                "runner",
                variants=tuple(f"v{variant:02d}" for variant in range(16)),
            )
            for index in range(32)
        ),
        profiles=(Profile("full", SelectionMode.FULL),),
    )
    planned = PlannedTests(
        POLICY_SHA, BINDINGS_SHA, SOURCE, plan_tests(policy, profile="full", changed_paths=())
    )
    receipts = _results(planned)
    receipts = replace(
        receipts, results=(replace(receipts.results[-1], outcome=ResultOutcome.FAILED),)
    )
    report = evaluate_results(planned, receipts, **EXPECTED)
    assert report.status == "failed" and not report.complete
    assert report.counts.selected_cells == report.diagnostic_count == 512
    assert report.counts.missing == 511 and report.counts.failed == 1
    assert len(report.diagnostics) == report.omitted_diagnostics == 256
    assert all(item.outcome is None for item in report.diagnostics)
    document = json.loads(encode_evaluation(report))
    assert document["diagnostic_count"] == 512 and document["omitted_diagnostics"] == 256


def test_sha256_git_objects_and_revision_only_source_are_supported(
    policy: QualificationPolicy,
) -> None:
    source = PlanSource(None, None, None, "a" * 64, "b" * 64)
    planned = PlannedTests(
        POLICY_SHA, BINDINGS_SHA, source, plan_tests(policy, profile="full", changed_paths=None)
    )
    expected: dict[str, Any] = {
        **EXPECTED,
        "expected_execution_commit": "a" * 64,
        "expected_execution_tree": "b" * 64,
    }
    report = evaluate_results(planned, _results(planned), **expected)
    assert report.passed and evaluation_document(report)["source"]["base_commit"] is None


def test_model_mutation_does_not_bypass_evaluator_validation(planned: PlannedTests) -> None:
    receipts = _results(planned)
    object.__setattr__(receipts, "attempt", True)
    with pytest.raises(InputError):
        evaluate_results(planned, receipts, **EXPECTED)
    receipts = _results(planned)
    object.__setattr__(receipts, "results", receipts.results + (receipts.results[0],))
    with pytest.raises(InputError):
        evaluate_results(planned, receipts, **EXPECTED)


def test_models_and_report_types_are_strict_and_reports_immutable(planned: PlannedTests) -> None:
    receipts = _results(planned)
    with pytest.raises(InputError):
        evaluate_results(cast(PlannedTests, {}), receipts, **EXPECTED)
    with pytest.raises(InputError):
        evaluate_results(planned, cast(Results, {}), **EXPECTED)
    with pytest.raises(InputError):
        evaluation_document(cast(Any, {}))
    report = evaluate_results(planned, receipts, **EXPECTED)
    with pytest.raises(FrozenInstanceError):
        cast(Any, report).run_id = "other"
    with pytest.raises(ValueError):
        replace(report.counts, passed=True)
    with pytest.raises(ValueError):
        replace(report.counts, received_cells=2)
    with pytest.raises(ValueError):
        replace(report, run_id="bad/run")
    with pytest.raises(ValueError):
        replace(report, bindings_sha256=cast(str, None))
    with pytest.raises(ValueError):
        EvaluationDiagnostic("unit", "default", "runner", ResultOutcome.PASSED)


def test_report_construction_rejects_inconsistent_or_duplicate_diagnostics(
    planned: PlannedTests,
) -> None:
    report = evaluate_results(planned, _results(planned, ResultOutcome.FAILED), **EXPECTED)
    with pytest.raises(ValueError):
        replace(report, diagnostics=report.diagnostics[:1])
    with pytest.raises(ValueError):
        replace(report, diagnostics=tuple(reversed(report.diagnostics)))
    with pytest.raises(ValueError):
        replace(report, diagnostics=(report.diagnostics[0],) * 3)
    with pytest.raises(ValueError):
        replace(
            report,
            diagnostics=tuple(
                replace(item, outcome=ResultOutcome.BLOCKED) for item in report.diagnostics
            ),
        )
    object.__setattr__(report.counts, "passed", 100)
    with pytest.raises(InputError):
        encode_evaluation(report)


def test_evaluation_bound_includes_final_newline_and_construction(
    planned: PlannedTests, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = evaluate_results(planned, _results(planned), **EXPECTED)
    encoded = encode_evaluation(report)
    monkeypatch.setattr(evaluation, "MAX_PLAN_BYTES", len(encoded))
    assert encode_evaluation(report) == encoded
    assert replace(report) == report
    monkeypatch.setattr(evaluation, "MAX_PLAN_BYTES", len(encoded) - 1)
    with pytest.raises(ConfigurationError, match="byte limit"):
        encode_evaluation(report)
    with pytest.raises(ConfigurationError, match="byte limit"):
        replace(report)
