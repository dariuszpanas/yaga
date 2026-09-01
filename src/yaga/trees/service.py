"""Installed committed-tree policy service shared by the Typer command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yaga.git import CommittedTreeIdentity
from yaga.trees.checker import check_tree
from yaga.trees.git import read_tree_paths
from yaga.trees.models import TreeReport
from yaga.trees.policy import load_tree_policy


@dataclass(frozen=True, slots=True)
class CheckedTree:
    """One checked committed tree together with its canonical policy source."""

    report: TreeReport
    policy_path: Path


def check_tree_policy(
    repository: Path,
    *,
    policy_path: Path,
    revision: str,
    identity: CommittedTreeIdentity | None = None,
) -> CheckedTree:
    """Load an explicit policy, read an exact committed tree, and check its paths."""
    loaded = load_tree_policy(policy_path)
    selection = (
        read_tree_paths(repository, revision)
        if identity is None
        else read_tree_paths(repository, revision, identity=identity)
    )
    report = check_tree(loaded.policy, selection)
    return CheckedTree(report=report, policy_path=loaded.path)
