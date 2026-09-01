"""Tests for the installed branch-policy service boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from yaga.branches import service
from yaga.branches.models import (
    BranchPolicy,
    BranchReport,
    LoadedBranchPolicy,
)
from yaga.branches.service import CheckedBranch


def test_branch_service_composes_loader_and_checker_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = (tmp_path / "branch-policy.toml").resolve()
    policy = BranchPolicy(branch_policy_version=1, allowed_patterns=("main", "feat/*"))
    loaded = LoadedBranchPolicy(policy=policy, path=policy_path)
    report = BranchReport(
        policy=policy,
        branch="feat/installed-layer",
        matched_pattern="feat/*",
        diagnostics=(),
    )
    calls: list[tuple[object, ...]] = []

    def fake_load(selected_path: Path) -> LoadedBranchPolicy:
        calls.append(("load", selected_path))
        return loaded

    def fake_check(selected_policy: BranchPolicy, branch: str) -> BranchReport:
        calls.append(("check", selected_policy, branch))
        return report

    monkeypatch.setattr(service, "load_branch_policy", fake_load)
    monkeypatch.setattr(service, "check_branch_name", fake_check)

    checked = service.check_branch(
        policy_path=Path("branch-policy.toml"),
        branch="feat/installed-layer",
    )

    assert checked == CheckedBranch(report=report, policy_path=policy_path)
    assert calls == [
        ("load", Path("branch-policy.toml")),
        ("check", policy, "feat/installed-layer"),
    ]


def test_checked_branch_is_frozen_and_requires_no_repository(tmp_path: Path) -> None:
    policy = BranchPolicy(branch_policy_version=1, allowed_patterns=("main",))
    checked = CheckedBranch(
        report=BranchReport(
            policy=policy,
            branch="main",
            matched_pattern="main",
            diagnostics=(),
        ),
        policy_path=(tmp_path / "policy.toml").resolve(),
    )

    with pytest.raises(AttributeError):
        checked.__setattr__("policy_path", tmp_path)
