"""Tests for provider-neutral named-lens aggregation."""

from __future__ import annotations

from typing import cast

import pytest

from yaga.agent_review.evaluation import LensOutcome, evaluate_policy
from yaga.agent_review.policy import AgentReviewPolicy, ReviewLens
from yaga.errors import ConfigurationError


def policy(aggregation: str = "all-required") -> AgentReviewPolicy:
    return AgentReviewPolicy(
        version=1,
        required=("correctness", "security"),
        aggregation=aggregation,
        lenses=(
            ReviewLens("correctness", "codex", "Review behavior.", "review"),
            ReviewLens("security", "codex", "Review trust.", "review"),
            ReviewLens("docs", "codex", "Review docs.", "advisory"),
        ),
    )


def test_all_required_keeps_advisory_failure_non_blocking() -> None:
    result = evaluate_policy(
        policy(),
        {"correctness": "passed", "security": "passed", "docs": "failed"},
    )

    assert result.state is LensOutcome.PASSED
    assert result.advisory_failures == ("docs",)


@pytest.mark.parametrize(
    ("outcomes", "state", "failures", "pending"),
    [
        ({"correctness": "failed", "security": "passed"}, "failed", ("correctness",), ()),
        ({"correctness": "passed"}, "pending", (), ("security",)),
    ],
)
def test_all_required_reports_blocking_details(
    outcomes: dict[str, str],
    state: str,
    failures: tuple[str, ...],
    pending: tuple[str, ...],
) -> None:
    result = evaluate_policy(policy(), outcomes)

    assert result.state.value == state
    assert result.blocking_failures == failures
    assert result.blocking_pending == pending


def test_any_required_passes_when_one_required_lens_passes() -> None:
    result = evaluate_policy(
        policy("any-required"),
        {"correctness": "failed", "security": "passed"},
    )

    assert result.state is LensOutcome.PASSED


def test_any_required_fails_only_when_every_required_lens_fails() -> None:
    result = evaluate_policy(
        policy("any-required"),
        {"correctness": "failed", "security": "failed"},
    )

    assert result.state is LensOutcome.FAILED


def test_evaluation_rejects_unknown_or_invalid_outcomes() -> None:
    with pytest.raises(ConfigurationError, match="unknown lens"):
        evaluate_policy(policy(), {"other": "passed"})
    with pytest.raises(ConfigurationError, match="is invalid"):
        evaluate_policy(policy(), {"correctness": "unknown"})
    with pytest.raises(ConfigurationError, match="names must be strings"):
        evaluate_policy(policy(), cast(dict[str, str], {1: "passed"}))
