"""Installed committed blob-size policy service shared by the Typer command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yaga.sizes.checker import check_sizes
from yaga.sizes.git import read_blob_sizes
from yaga.sizes.models import SizeReport
from yaga.sizes.policy import load_size_policy


@dataclass(frozen=True, slots=True)
class CheckedSize:
    """One checked committed tree together with its canonical policy source."""

    report: SizeReport
    policy_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.report, SizeReport):
            raise ValueError("checked size result requires a SizeReport")
        if not isinstance(self.policy_path, Path) or not self.policy_path.is_absolute():
            raise ValueError("checked size result policy path must be absolute")


def check_size_policy(
    repository: Path,
    *,
    policy_path: Path,
    revision: str,
) -> CheckedSize:
    """Load policy, read one exact committed tree, and check its blob sizes."""
    loaded = load_size_policy(policy_path)
    selection = read_blob_sizes(repository, revision)
    report = check_sizes(loaded.policy, selection)
    return CheckedSize(report=report, policy_path=loaded.path)
