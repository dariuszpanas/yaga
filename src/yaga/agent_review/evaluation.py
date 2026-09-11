"""Provider-neutral evaluation of named Agent review outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from yaga.agent_review.policy import AgentReviewPolicy
from yaga.errors import ConfigurationError


class LensOutcome(StrEnum):
    """Closed outcome states emitted by provider adapters."""

    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class ReviewAggregate:
    """Deterministic policy result with separate blocking and advisory details."""

    state: LensOutcome
    blocking_failures: tuple[str, ...]
    blocking_pending: tuple[str, ...]
    advisory_failures: tuple[str, ...]
    advisory_pending: tuple[str, ...]


def evaluate_policy(
    policy: AgentReviewPolicy,
    outcomes: Mapping[str, LensOutcome | str],
) -> ReviewAggregate:
    """Aggregate exact named-lens outcomes without letting advisory lenses block."""
    if not isinstance(policy, AgentReviewPolicy):
        raise TypeError("policy must be an AgentReviewPolicy")
    if not isinstance(outcomes, Mapping):
        raise TypeError("outcomes must be a mapping")
    if any(not isinstance(name, str) for name in outcomes):
        raise ConfigurationError("Agent review outcome names must be strings")
    known = {lens.name for lens in policy.lenses}
    unknown = sorted(set(outcomes) - known)
    if unknown:
        raise ConfigurationError(f"Agent review outcome names unknown lens {unknown[0]}")
    normalized: dict[str, LensOutcome] = {}
    for name, value in outcomes.items():
        try:
            normalized[name] = LensOutcome(value)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(f"Agent review outcome for {name} is invalid") from error

    advisory_names = tuple(lens.name for lens in policy.lenses if lens.name not in policy.required)
    blocking_failures = tuple(
        name for name in policy.required if normalized.get(name) is LensOutcome.FAILED
    )
    blocking_pending = tuple(
        name
        for name in policy.required
        if normalized.get(name, LensOutcome.PENDING) is LensOutcome.PENDING
    )
    advisory_failures = tuple(
        name for name in advisory_names if normalized.get(name) is LensOutcome.FAILED
    )
    advisory_pending = tuple(
        name
        for name in advisory_names
        if normalized.get(name, LensOutcome.PENDING) is LensOutcome.PENDING
    )
    if policy.aggregation == "all-required":
        state = (
            LensOutcome.FAILED
            if blocking_failures
            else LensOutcome.PENDING
            if blocking_pending
            else LensOutcome.PASSED
        )
    else:
        state = (
            LensOutcome.PASSED
            if any(normalized.get(name) is LensOutcome.PASSED for name in policy.required)
            else LensOutcome.FAILED
            if not blocking_pending and len(blocking_failures) == len(policy.required)
            else LensOutcome.PENDING
        )
    return ReviewAggregate(
        state,
        blocking_failures,
        blocking_pending,
        advisory_failures,
        advisory_pending,
    )
