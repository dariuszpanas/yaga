"""Installed committed-mode policy service shared by the Typer command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yaga.modes.checker import check_modes
from yaga.modes.git import read_mode_selection
from yaga.modes.models import ModeReport
from yaga.modes.policy import load_mode_policy


@dataclass(frozen=True, slots=True)
class CheckedMode:
    """One checked committed tree together with its canonical policy source."""

    report: ModeReport
    policy_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.report, ModeReport):
            raise ValueError("checked mode result requires a ModeReport")
        if not isinstance(self.policy_path, Path) or not self.policy_path.is_absolute():
            raise ValueError("checked mode result policy path must be absolute")


def check_mode_policy(
    repository: Path,
    *,
    policy_path: Path,
    revision: str,
) -> CheckedMode:
    """Load policy, read one exact committed tree, and check its entry modes."""
    loaded = load_mode_policy(policy_path)
    selection = read_mode_selection(repository, revision)
    report = check_modes(loaded.policy, selection)
    return CheckedMode(report=report, policy_path=loaded.path)
