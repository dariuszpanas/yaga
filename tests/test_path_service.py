"""Tests for the installed committed-path policy service boundary."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from yaga.git import CommittedTreeIdentity
from yaga.paths import service
from yaga.paths.checker import check_paths
from yaga.paths.models import LoadedPathPolicy, PathPolicy, PathReport, PathSelection
from yaga.paths.service import CheckedPath


def _selection(repository: Path) -> PathSelection:
    return PathSelection(
        repository=repository.resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=("README.md", "src/main.py"),
    )


@pytest.mark.parametrize("reuse_identity", [False, True])
def test_path_service_composes_loader_git_and_checker_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reuse_identity: bool,
) -> None:
    repository = tmp_path / "repository"
    policy_path = (tmp_path / "path-policy.toml").resolve()
    policy = PathPolicy(1, ("windows-reserved",))
    loaded = LoadedPathPolicy(policy=policy, path=policy_path)
    selection = _selection(repository)
    report = check_paths(policy, selection)
    identity = cast(CommittedTreeIdentity, object())
    calls: list[tuple[object, ...]] = []

    def fake_load(selected_path: Path) -> LoadedPathPolicy:
        calls.append(("load", selected_path))
        return loaded

    def fake_read(
        selected_repository: Path,
        revision: str,
        *,
        identity: CommittedTreeIdentity | None = None,
    ) -> PathSelection:
        calls.append(("git", selected_repository, revision, identity))
        return selection

    def fake_check(
        selected_policy: PathPolicy,
        selected_tree: PathSelection,
    ) -> PathReport:
        calls.append(("check", selected_policy, selected_tree))
        return report

    monkeypatch.setattr(service, "load_path_policy", fake_load)
    monkeypatch.setattr(service, "read_path_selection", fake_read)
    monkeypatch.setattr(service, "check_paths", fake_check)

    checked = service.check_path_policy(
        repository,
        policy_path=Path("path-policy.toml"),
        revision="release-candidate",
        identity=identity if reuse_identity else None,
    )

    assert checked == CheckedPath(report=report, policy_path=policy_path)
    assert calls == [
        ("load", Path("path-policy.toml")),
        ("git", repository, "release-candidate", identity if reuse_identity else None),
        ("check", policy, selection),
    ]


def test_checked_path_is_frozen_and_carries_canonical_sources(tmp_path: Path) -> None:
    policy = PathPolicy(1, ("windows-reserved",))
    checked = CheckedPath(
        report=check_paths(policy, _selection(tmp_path / "repository")),
        policy_path=(tmp_path / "policy.toml").resolve(),
    )

    assert checked.report.selection.repository == (tmp_path / "repository").resolve()
    assert checked.policy_path == (tmp_path / "policy.toml").resolve()
    with pytest.raises(AttributeError):
        checked.__setattr__("policy_path", tmp_path)


@pytest.mark.parametrize(
    "report",
    [None, "report", object()],
)
def test_checked_path_rejects_non_report_values(tmp_path: Path, report: object) -> None:
    with pytest.raises(ValueError, match="PathReport"):
        CheckedPath(
            report=cast(PathReport, report),
            policy_path=(tmp_path / "policy.toml").resolve(),
        )


@pytest.mark.parametrize(
    "policy_path",
    [Path("policy.toml"), "policy.toml", None],
)
def test_checked_path_requires_an_absolute_path_source(
    tmp_path: Path,
    policy_path: object,
) -> None:
    policy = PathPolicy(1, ("windows-reserved",))
    report = check_paths(policy, _selection(tmp_path / "repository"))

    with pytest.raises(ValueError, match="absolute"):
        CheckedPath(report=report, policy_path=cast(Path, policy_path))
