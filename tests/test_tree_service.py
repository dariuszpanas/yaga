"""Tests for the installed committed-tree policy service boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from yaga.trees import service
from yaga.trees.models import (
    LoadedTreePolicy,
    TreePolicy,
    TreeReport,
    TreeSelection,
)
from yaga.trees.service import CheckedTree


def _selection(repository: Path) -> TreeSelection:
    return TreeSelection(
        repository=repository.resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=("README.md", "src/main.py"),
    )


def test_tree_service_composes_loader_git_and_checker_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    policy_path = (tmp_path / "tree-policy.toml").resolve()
    policy = TreePolicy(
        tree_policy_version=1,
        required_paths=("README.md",),
        forbidden_patterns=("secrets/**",),
    )
    loaded = LoadedTreePolicy(policy=policy, path=policy_path)
    selection = _selection(repository)
    report = TreeReport(policy=policy, selection=selection, diagnostics=())
    calls: list[tuple[object, ...]] = []

    def fake_load(selected_path: Path) -> LoadedTreePolicy:
        calls.append(("load", selected_path))
        return loaded

    def fake_read(selected_repository: Path, revision: str) -> TreeSelection:
        calls.append(("git", selected_repository, revision))
        return selection

    def fake_check(
        selected_policy: TreePolicy,
        selected_tree: TreeSelection,
    ) -> TreeReport:
        calls.append(("check", selected_policy, selected_tree))
        return report

    monkeypatch.setattr(service, "load_tree_policy", fake_load)
    monkeypatch.setattr(service, "read_tree_paths", fake_read)
    monkeypatch.setattr(service, "check_tree", fake_check)

    checked = service.check_tree_policy(
        repository,
        policy_path=Path("tree-policy.toml"),
        revision="release-candidate",
    )

    assert checked == CheckedTree(report=report, policy_path=policy_path)
    assert calls == [
        ("load", Path("tree-policy.toml")),
        ("git", repository, "release-candidate"),
        ("check", policy, selection),
    ]


def test_checked_tree_is_frozen_and_carries_canonical_sources(tmp_path: Path) -> None:
    policy = TreePolicy(
        tree_policy_version=1,
        required_paths=("README.md",),
        forbidden_patterns=(),
    )
    checked = CheckedTree(
        report=TreeReport(
            policy=policy,
            selection=_selection(tmp_path / "repository"),
            diagnostics=(),
        ),
        policy_path=(tmp_path / "policy.toml").resolve(),
    )

    assert checked.report.selection.repository == (tmp_path / "repository").resolve()
    with pytest.raises(AttributeError):
        checked.__setattr__("policy_path", tmp_path)
