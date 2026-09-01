"""Installed changed-path policy service shared by the Typer command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yaga.changes.checker import check_changed_paths
from yaga.changes.git import read_changed_paths
from yaga.changes.models import ChangeReport
from yaga.changes.policy import load_change_policy


@dataclass(frozen=True, slots=True)
class CheckedChanges:
    """One checked range together with its canonical policy source."""

    report: ChangeReport
    policy_path: Path


def check_changes(
    repository: Path,
    *,
    policy_path: Path,
    revision_range: str,
) -> CheckedChanges:
    """Load one explicit policy, read one exact Git range, and check its paths."""
    loaded = load_change_policy(policy_path)
    selection = read_changed_paths(repository, revision_range)
    report = check_changed_paths(loaded.policy, selection)
    return CheckedChanges(report=report, policy_path=loaded.path)
