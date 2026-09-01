"""Installed committed-path policy service shared by the Typer command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yaga.git import CommittedTreeIdentity
from yaga.paths.checker import check_paths
from yaga.paths.git import read_path_selection
from yaga.paths.models import PathReport
from yaga.paths.policy import load_path_policy


@dataclass(frozen=True, slots=True)
class CheckedPath:
    """One checked committed tree together with its canonical policy source."""

    report: PathReport
    policy_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.report, PathReport):
            raise ValueError("checked path result requires a PathReport")
        if not isinstance(self.policy_path, Path) or not self.policy_path.is_absolute():
            raise ValueError("checked path result policy path must be absolute")


def check_path_policy(
    repository: Path,
    *,
    policy_path: Path,
    revision: str,
    identity: CommittedTreeIdentity | None = None,
) -> CheckedPath:
    """Load policy, read one exact committed tree, and check its path names."""
    loaded = load_path_policy(policy_path)
    selection = (
        read_path_selection(repository, revision)
        if identity is None
        else read_path_selection(repository, revision, identity=identity)
    )
    report = check_paths(loaded.policy, selection)
    return CheckedPath(report=report, policy_path=loaded.path)
