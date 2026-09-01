"""Tests for immutable branch-policy models and normalized state."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

from yaga.branches.models import (
    BRANCH_ALLOWED_CODE,
    BRANCH_SYNTAX_CODE,
    MAX_BRANCH_PATTERNS,
    BranchDiagnostic,
    BranchPolicy,
    BranchReport,
    LoadedBranchPolicy,
)


def policy(*patterns: str) -> BranchPolicy:
    return BranchPolicy(branch_policy_version=1, allowed_patterns=patterns)


def test_policy_loaded_policy_and_reports_preserve_exact_normalized_values(
    tmp_path: Path,
) -> None:
    configured = policy("feat/**", "Feature/*")
    source = (tmp_path / "branch-policy.toml").resolve()
    loaded = LoadedBranchPolicy(policy=configured, path=source)
    passed = BranchReport(configured, "feat/topic", "feat/**", ())
    failed = BranchReport(
        configured,
        "fix/topic",
        None,
        (BranchDiagnostic(BRANCH_ALLOWED_CODE, "not allowed"),),
    )

    assert loaded.policy is configured
    assert loaded.path == source
    assert passed.status == "passed"
    assert passed.valid
    assert failed.status == "failed"
    assert not failed.valid


def test_models_are_frozen_and_slotted() -> None:
    configured = policy("main")
    diagnostic = BranchDiagnostic(BRANCH_ALLOWED_CODE, "not allowed")
    report = BranchReport(configured, "other", None, (diagnostic,))

    for value in (configured, diagnostic, report):
        assert not hasattr(value, "__dict__")
    with pytest.raises(FrozenInstanceError):
        configured.__setattr__("allowed_patterns", ("**",))


@pytest.mark.parametrize("version", [True, False, 0, 2, 1.0, "1"])
def test_policy_version_is_exact_integer_one(version: object) -> None:
    with pytest.raises(ValueError, match="version"):
        BranchPolicy(cast(int, version), ("main",))


def test_policy_pattern_collection_is_tuple_nonempty_bounded_and_exactly_unique() -> None:
    exact = tuple(f"branch-{index}" for index in range(MAX_BRANCH_PATTERNS))

    assert len(BranchPolicy(1, exact).allowed_patterns) == MAX_BRANCH_PATTERNS
    assert BranchPolicy(1, ("feat/*", "Feat/*")).allowed_patterns == (
        "feat/*",
        "Feat/*",
    )
    for value in (
        cast(tuple[str, ...], []),
        (),
        exact + ("overflow",),
        ("main", cast(str, 1)),
    ):
        with pytest.raises(ValueError, match="allowed patterns"):
            BranchPolicy(1, value)
    with pytest.raises(ValueError, match="unique"):
        BranchPolicy(1, ("main", "main"))
    with pytest.raises(ValueError, match="branch pattern"):
        BranchPolicy(1, ("bad pattern",))


def test_loaded_policy_requires_exact_model_and_absolute_path(tmp_path: Path) -> None:
    configured = policy("main")

    with pytest.raises(ValueError, match="BranchPolicy"):
        LoadedBranchPolicy(cast(BranchPolicy, object()), tmp_path.resolve())
    with pytest.raises(ValueError, match="absolute"):
        LoadedBranchPolicy(configured, Path("relative.toml"))


def test_diagnostic_and_report_models_reject_inconsistent_states() -> None:
    configured = policy("feat/**")
    allowed = BranchDiagnostic(BRANCH_ALLOWED_CODE, "not allowed")
    syntax = BranchDiagnostic(BRANCH_SYNTAX_CODE, "bad syntax")

    with pytest.raises(ValueError, match="code"):
        BranchDiagnostic("branch.unknown", "unknown")
    with pytest.raises(ValueError, match="code"):
        BranchDiagnostic(cast(str, []), "unknown")
    with pytest.raises(ValueError, match="message"):
        BranchDiagnostic(BRANCH_ALLOWED_CODE, "")
    with pytest.raises(ValueError, match="at most one"):
        BranchReport(configured, "fix/topic", None, (allowed, allowed))
    with pytest.raises(ValueError, match="matched pattern"):
        BranchReport(configured, "fix/topic", "feat/**", (allowed,))
    with pytest.raises(ValueError, match="syntax"):
        BranchReport(configured, "fix/topic", None, (syntax,))
    with pytest.raises(ValueError, match="syntax"):
        BranchReport(configured, "bad name", None, (allowed,))
    with pytest.raises(ValueError, match="configured matched pattern"):
        BranchReport(configured, "feat/topic", None, ())
    with pytest.raises(ValueError, match="configured matched pattern"):
        BranchReport(configured, "feat/topic", "other/**", ())

    assert BranchReport(configured, "bad name", None, (syntax,)).diagnostics == (syntax,)


@pytest.mark.parametrize("branch", ["", "a" * 245, "\ud800"])
def test_report_requires_transport_valid_branch_even_for_syntax_findings(branch: str) -> None:
    configured = policy("**")
    syntax = BranchDiagnostic(BRANCH_SYNTAX_CODE, "bad syntax")

    with pytest.raises(ValueError, match="branch name"):
        BranchReport(configured, branch, None, (syntax,))
