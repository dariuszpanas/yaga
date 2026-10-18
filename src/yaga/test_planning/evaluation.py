"""Pure correlation of complete test-plan obligations with adapter result receipts."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from yaga.errors import InputError
from yaga.test_planning.diagnostics import parse_plan_document
from yaga.test_planning.limits import MAX_CELLS, MAX_PLAN_BYTES
from yaga.test_planning.models import Suite
from yaga.test_planning.reporting import test_plan_document
from yaga.test_planning.reports import PlannedTests
from yaga.test_planning.results import (
    ResultOutcome,
    TestResults,
    _encode_document,
    encode_results,
    parse_results_document,
    results_document,
    validate_digest,
    validate_execution_identity,
    validate_run_identity,
)
from yaga.test_planning.source import PlanSource

TEST_EVALUATION_VERSION = 1
MAX_EVALUATION_DIAGNOSTICS = 256
_DIAGNOSTICS = {
    None: ("test.required-result", "Required cell has no result."),
    ResultOutcome.PENDING: ("test.required-result", "Required cell is pending."),
    ResultOutcome.BLOCKED: ("test.required-result", "Required cell is blocked."),
    ResultOutcome.FAILED: ("test.failed", "Required cell failed."),
    ResultOutcome.CANCELLED: ("test.cancelled", "Required cell was cancelled."),
}


@dataclass(frozen=True, slots=True)
class EvaluationCounts:
    """Exact complete counts, including obligations without a receipt."""

    selected_cells: int
    received_cells: int
    passed: int
    failed: int
    pending: int
    cancelled: int
    blocked: int
    missing: int

    def __post_init__(self) -> None:
        values = (
            self.selected_cells,
            self.received_cells,
            self.passed,
            self.failed,
            self.pending,
            self.cancelled,
            self.blocked,
            self.missing,
        )
        if any(type(value) is not int or not 0 <= value <= MAX_CELLS for value in values):
            raise ValueError("evaluation counts must be bounded nonnegative cell counts")
        if (
            self.received_cells
            != self.passed + self.failed + self.pending + self.cancelled + self.blocked
        ):
            raise ValueError("evaluation received count must equal its outcome counts")
        if self.selected_cells != self.received_cells + self.missing:
            raise ValueError("evaluation selected count must include every missing cell")


@dataclass(frozen=True, slots=True)
class EvaluationDiagnostic:
    """One non-passing obligation with a fixed, safe human explanation."""

    suite: str
    variant: str
    binding: str
    outcome: ResultOutcome | None

    def __post_init__(self) -> None:
        Suite(self.suite, self.binding, variants=(self.variant,))
        if self.outcome is not None and (
            type(self.outcome) is not ResultOutcome or self.outcome is ResultOutcome.PASSED
        ):
            raise ValueError("evaluation diagnostics require missing or non-passed outcomes")

    @property
    def code(self) -> str:
        """Return the stable diagnostic code for this recorded outcome."""
        return _DIAGNOSTICS[self.outcome][0]

    @property
    def message(self) -> str:
        """Return fixed text; adapter summaries are never interpolated into it."""
        return _DIAGNOSTICS[self.outcome][1]


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Correlated receipt results, not authentication or independent test execution.

    Complete means every selected cell has a terminal outcome; failed or
    cancelled cells may be complete, but never passed. Empty valid selections
    have zero counts and satisfy this plan without claiming that any test ran.
    """

    plan_digest: str
    policy_sha256: str
    bindings_sha256: str
    results_sha256: str
    source: PlanSource
    run_id: str
    attempt: int
    counts: EvaluationCounts
    diagnostics: tuple[EvaluationDiagnostic, ...]

    def __post_init__(self) -> None:
        _validate_report(self)
        _encode_document(_document(self), maximum=MAX_PLAN_BYTES)

    @property
    def complete(self) -> bool:
        """Whether there are no missing, pending, or blocked obligations."""
        return not (self.counts.missing or self.counts.pending or self.counts.blocked)

    @property
    def status(self) -> str:
        """Return failed before incomplete; passed requires every cell to pass."""
        if self.counts.failed or self.counts.cancelled:
            return "failed"
        return "passed" if self.complete else "incomplete"

    @property
    def passed(self) -> bool:
        """Whether all obligations of this exact plan have passing declarations."""
        return self.status == "passed"

    @property
    def exit_code(self) -> int:
        """A well-formed evaluation uses exit zero or one; input errors raise."""
        return 0 if self.passed else 1

    @property
    def diagnostic_count(self) -> int:
        """Count every non-passing cell, including diagnostics omitted from display."""
        return self.counts.selected_cells - self.counts.passed

    @property
    def omitted_diagnostics(self) -> int:
        """Count diagnostics beyond the fixed stored-report limit exactly."""
        return self.diagnostic_count - len(self.diagnostics)


def evaluate_results(
    planned: PlannedTests,
    results: TestResults,
    *,
    expected_policy_sha256: str,
    expected_bindings_sha256: str,
    expected_execution_commit: str,
    expected_execution_tree: str,
    expected_run_id: str,
    expected_attempt: int,
) -> EvaluationReport:
    """Check all selected cells against independent expected source/run identities.

    The caller authenticates the expected plan, CI provenance, manifest use,
    custom acceptance, and cleanup before declaring passed cells. This evaluator
    does not read artifacts, execute verifiers, or select a green retry record.
    """
    if type(planned) is not PlannedTests or type(results) is not TestResults:
        raise InputError("test evaluation requires validated plan and results models")
    try:
        validate_digest(expected_policy_sha256)
        validate_digest(expected_bindings_sha256)
        validate_execution_identity(expected_execution_commit, expected_execution_tree)
        validate_run_identity(expected_run_id, expected_attempt)
        PlannedTests(
            planned.policy_sha256, planned.bindings_sha256, planned.source, planned.selection
        )
    except (TypeError, ValueError) as error:
        raise InputError("invalid test evaluation model or expected identity") from error
    planned = parse_plan_document(test_plan_document(planned))
    results = parse_results_document(results_document(results))
    plan = test_plan_document(planned)
    if planned.bindings_sha256 is None:
        raise InputError("preview test plans without a binding identity cannot qualify")
    if (
        planned.policy_sha256 != expected_policy_sha256
        or results.policy_sha256 != expected_policy_sha256
    ):
        raise InputError("test evaluation policy identity does not match the expected policy")
    if (
        planned.bindings_sha256 != expected_bindings_sha256
        or results.bindings_sha256 != expected_bindings_sha256
    ):
        raise InputError("test evaluation binding identity does not match the expected manifest")
    if (
        planned.source.execution_commit != expected_execution_commit
        or results.execution_commit != expected_execution_commit
        or planned.source.execution_tree != expected_execution_tree
        or results.execution_tree != expected_execution_tree
    ):
        raise InputError("test evaluation source does not match the expected execution identity")
    if results.run_id != expected_run_id or results.attempt != expected_attempt:
        raise InputError(
            "test evaluation run or aggregate attempt does not match the expected identity"
        )
    if results.plan_digest != plan["plan_digest"]:
        raise InputError("test result plan digest does not match the expected plan")

    catalog = {suite.id: suite for suite in planned.selection.inventory}
    required = {
        (item.suite, variant): catalog[item.suite].binding
        for item in planned.selection.selected
        for variant in item.variants
    }
    received = {(item.suite, item.variant): item for item in results.results}
    if any(
        key not in required or result.binding != required[key] for key, result in received.items()
    ):
        raise InputError("test results contain an unknown, unselected, or incorrectly bound cell")
    outcomes: Counter[ResultOutcome] = Counter()
    missing = 0
    diagnostics: list[EvaluationDiagnostic] = []
    for (suite, variant), binding in sorted(required.items()):
        result = received.get((suite, variant))
        outcome = result.outcome if result is not None else None
        if outcome is None:
            missing += 1
        else:
            outcomes[outcome] += 1
        if outcome is not ResultOutcome.PASSED and len(diagnostics) < MAX_EVALUATION_DIAGNOSTICS:
            diagnostics.append(EvaluationDiagnostic(suite, variant, binding, outcome))
    return EvaluationReport(
        plan_digest=plan["plan_digest"],
        policy_sha256=planned.policy_sha256,
        bindings_sha256=planned.bindings_sha256,
        results_sha256=hashlib.sha256(encode_results(results)[:-1]).hexdigest(),
        source=planned.source,
        run_id=results.run_id,
        attempt=results.attempt,
        counts=EvaluationCounts(
            selected_cells=len(required),
            received_cells=len(received),
            missing=missing,
            passed=outcomes[ResultOutcome.PASSED],
            failed=outcomes[ResultOutcome.FAILED],
            pending=outcomes[ResultOutcome.PENDING],
            cancelled=outcomes[ResultOutcome.CANCELLED],
            blocked=outcomes[ResultOutcome.BLOCKED],
        ),
        diagnostics=tuple(diagnostics),
    )


def evaluation_document(report: EvaluationReport) -> dict[str, Any]:
    """Return the closed v1 report with exact counts and bounded diagnostics."""
    if type(report) is not EvaluationReport:
        raise InputError("evaluation encoding requires an EvaluationReport model")
    try:
        _validate_report(report)
    except (TypeError, ValueError) as error:
        raise InputError("evaluation encoding requires a valid report model") from error
    document = _document(report)
    _encode_document(document, maximum=MAX_PLAN_BYTES)
    return document


def encode_evaluation(report: EvaluationReport) -> bytes:
    """Encode a complete report as bounded canonical UTF-8 JSON with newline."""
    return _encode_document(evaluation_document(report), maximum=MAX_PLAN_BYTES)


def _document(report: EvaluationReport) -> dict[str, Any]:
    return {
        "test-evaluation-version": TEST_EVALUATION_VERSION,
        "plan_digest": report.plan_digest,
        "policy_sha256": report.policy_sha256,
        "bindings_sha256": report.bindings_sha256,
        "results_sha256": report.results_sha256,
        "source": asdict(report.source),
        "run_id": report.run_id,
        "attempt": report.attempt,
        "status": report.status,
        "complete": report.complete,
        "passed": report.passed,
        "exit_code": report.exit_code,
        "counts": asdict(report.counts),
        "diagnostic_count": report.diagnostic_count,
        "omitted_diagnostics": report.omitted_diagnostics,
        "diagnostics": [
            {
                "code": item.code,
                "suite": item.suite,
                "variant": item.variant,
                "binding": item.binding,
                "outcome": item.outcome.value if item.outcome is not None else None,
                "message": item.message,
            }
            for item in report.diagnostics
        ],
    }


def _validate_report(report: EvaluationReport) -> None:
    for digest in (
        report.plan_digest,
        report.policy_sha256,
        report.bindings_sha256,
        report.results_sha256,
    ):
        validate_digest(digest)
    if type(report.source) is not PlanSource:
        raise ValueError("evaluation source must be a PlanSource")
    source = asdict(report.source)
    if any(value is not None and type(value) is not str for value in source.values()):
        raise ValueError(
            "evaluation source identities must be strings or explicit null comparisons"
        )
    PlanSource(**source)
    validate_run_identity(report.run_id, report.attempt)
    if type(report.counts) is not EvaluationCounts:
        raise ValueError("evaluation report requires EvaluationCounts")
    EvaluationCounts(**asdict(report.counts))
    if (
        type(report.diagnostics) is not tuple
        or len(report.diagnostics) != min(report.diagnostic_count, MAX_EVALUATION_DIAGNOSTICS)
        or any(type(item) is not EvaluationDiagnostic for item in report.diagnostics)
    ):
        raise ValueError("evaluation diagnostics must retain the bounded exact required count")
    keys: list[tuple[str, str]] = []
    outcomes: Counter[str] = Counter()
    for item in report.diagnostics:
        EvaluationDiagnostic(item.suite, item.variant, item.binding, item.outcome)
        keys.append((item.suite, item.variant))
        outcomes[item.outcome.value if item.outcome is not None else "missing"] += 1
    if keys != sorted(set(keys)):
        raise ValueError("evaluation diagnostics require unique canonically ordered cells")
    for outcome in ("failed", "pending", "cancelled", "blocked", "missing"):
        count = getattr(report.counts, outcome)
        if outcomes[outcome] > count or (
            not report.omitted_diagnostics and outcomes[outcome] != count
        ):
            raise ValueError("evaluation diagnostics do not agree with exact outcome counts")
