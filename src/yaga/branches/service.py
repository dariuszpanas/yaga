"""Installed branch-policy service shared by the Typer command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yaga.branches.checker import check_branch_name
from yaga.branches.models import BranchReport
from yaga.branches.policy import load_branch_policy


@dataclass(frozen=True, slots=True)
class CheckedBranch:
    """One checked branch together with its canonical policy source."""

    report: BranchReport
    policy_path: Path


def check_branch(*, policy_path: Path, branch: str) -> CheckedBranch:
    """Load one explicit policy and check one explicit short branch name."""
    loaded = load_branch_policy(policy_path)
    report = check_branch_name(loaded.policy, branch)
    return CheckedBranch(report=report, policy_path=loaded.path)
