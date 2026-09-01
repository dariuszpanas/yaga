"""Tests for the installed committed blob-size service boundary."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from yaga.git import CommittedTreeIdentity
from yaga.sizes import service
from yaga.sizes.models import (
    BlobEntry,
    LoadedSizePolicy,
    SizePolicy,
    SizeReport,
    SizeSelection,
)
from yaga.sizes.service import CheckedSize


@pytest.mark.parametrize("reuse_identity", [False, True])
def test_size_service_composes_loader_git_and_checker_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reuse_identity: bool,
) -> None:
    repository = tmp_path / "repository"
    policy_path = (tmp_path / "size-policy.toml").resolve()
    policy = SizePolicy(1, 100)
    loaded = LoadedSizePolicy(policy=policy, path=policy_path)
    selection = SizeSelection(
        repository=repository.resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        blobs=(BlobEntry("README.md", "c" * 40, "100644", 25),),
        gitlinks=(),
    )
    report = SizeReport(policy, selection, ())
    identity = cast(CommittedTreeIdentity, object())
    calls: list[tuple[object, ...]] = []

    def fake_load(selected_path: Path) -> LoadedSizePolicy:
        calls.append(("load", selected_path))
        return loaded

    def fake_read(
        selected_repository: Path,
        revision: str,
        *,
        identity: CommittedTreeIdentity | None = None,
    ) -> SizeSelection:
        calls.append(("git", selected_repository, revision, identity))
        return selection

    def fake_check(selected_policy: SizePolicy, selected_tree: SizeSelection) -> SizeReport:
        calls.append(("check", selected_policy, selected_tree))
        return report

    monkeypatch.setattr(service, "load_size_policy", fake_load)
    monkeypatch.setattr(service, "read_blob_sizes", fake_read)
    monkeypatch.setattr(service, "check_sizes", fake_check)

    checked = service.check_size_policy(
        repository,
        policy_path=Path("size-policy.toml"),
        revision="release-candidate",
        identity=identity if reuse_identity else None,
    )

    assert checked == CheckedSize(report=report, policy_path=policy_path)
    assert calls == [
        ("load", Path("size-policy.toml")),
        ("git", repository, "release-candidate", identity if reuse_identity else None),
        ("check", policy, selection),
    ]


def test_checked_size_requires_frozen_canonical_sources(tmp_path: Path) -> None:
    policy = SizePolicy(1, 100)
    selection = SizeSelection(tmp_path.resolve(), "HEAD", "a" * 40, "b" * 40, (), ())
    checked = CheckedSize(
        report=SizeReport(policy, selection, ()),
        policy_path=(tmp_path / "policy.toml").resolve(),
    )

    with pytest.raises(AttributeError):
        checked.__setattr__("policy_path", tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        CheckedSize(checked.report, Path("policy.toml"))
