"""Tests for the installed committed-mode policy service boundary."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from yaga.modes import service
from yaga.modes.checker import check_modes
from yaga.modes.models import (
    LoadedModePolicy,
    ModeEntry,
    ModeKind,
    ModePolicy,
    ModeReport,
    ModeSelection,
)
from yaga.modes.service import CheckedMode


def _selection(repository: Path) -> ModeSelection:
    return ModeSelection(
        repository=repository.resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        entries=(ModeEntry("README.md", "c" * 40, "100644", "blob"),),
    )


def test_mode_service_composes_loader_git_and_checker_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    policy_path = (tmp_path / "mode-policy.toml").resolve()
    policy = ModePolicy(1, (ModeKind.REGULAR,))
    loaded = LoadedModePolicy(policy=policy, path=policy_path)
    selection = _selection(repository)
    report = check_modes(policy, selection)
    calls: list[tuple[object, ...]] = []

    def fake_load(selected_path: Path) -> LoadedModePolicy:
        calls.append(("load", selected_path))
        return loaded

    def fake_read(selected_repository: Path, revision: str) -> ModeSelection:
        calls.append(("git", selected_repository, revision))
        return selection

    def fake_check(selected_policy: ModePolicy, selected_tree: ModeSelection) -> ModeReport:
        calls.append(("check", selected_policy, selected_tree))
        return report

    monkeypatch.setattr(service, "load_mode_policy", fake_load)
    monkeypatch.setattr(service, "read_mode_selection", fake_read)
    monkeypatch.setattr(service, "check_modes", fake_check)

    checked = service.check_mode_policy(
        repository,
        policy_path=Path("mode-policy.toml"),
        revision="release-candidate",
    )

    assert checked == CheckedMode(report=report, policy_path=policy_path)
    assert calls == [
        ("load", Path("mode-policy.toml")),
        ("git", repository, "release-candidate"),
        ("check", policy, selection),
    ]


def test_checked_mode_is_frozen_and_carries_canonical_sources(tmp_path: Path) -> None:
    policy = ModePolicy(1, (ModeKind.REGULAR,))
    checked = CheckedMode(
        report=check_modes(policy, _selection(tmp_path / "repository")),
        policy_path=(tmp_path / "policy.toml").resolve(),
    )

    assert checked.report.selection.repository == (tmp_path / "repository").resolve()
    assert checked.policy_path == (tmp_path / "policy.toml").resolve()
    with pytest.raises(AttributeError):
        checked.__setattr__("policy_path", tmp_path)


@pytest.mark.parametrize("report", [None, "report", object()])
def test_checked_mode_rejects_non_report_values(tmp_path: Path, report: object) -> None:
    with pytest.raises(ValueError, match="ModeReport"):
        CheckedMode(
            report=cast(ModeReport, report),
            policy_path=(tmp_path / "policy.toml").resolve(),
        )


@pytest.mark.parametrize("policy_path", [Path("policy.toml"), "policy.toml", None])
def test_checked_mode_requires_an_absolute_path_source(
    tmp_path: Path,
    policy_path: object,
) -> None:
    policy = ModePolicy(1, (ModeKind.REGULAR,))
    report = check_modes(policy, _selection(tmp_path / "repository"))

    with pytest.raises(ValueError, match="absolute"):
        CheckedMode(report=report, policy_path=cast(Path, policy_path))
