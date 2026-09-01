"""Tests for versioned aggregate repository models."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

from yaga.modes.checker import check_modes
from yaga.modes.models import ModeEntry, ModeKind, ModePolicy, ModeSelection
from yaga.modes.service import CheckedMode
from yaga.paths.checker import check_paths
from yaga.paths.models import PathPolicy, PathSelection
from yaga.paths.service import CheckedPath
from yaga.repository.models import (
    REPOSITORY_PLAN_V1_PROVIDER_ORDER,
    REPOSITORY_PLAN_V1_PROVIDERS,
    REPOSITORY_PLAN_V2_PROVIDER_ORDER,
    REPOSITORY_PLAN_V2_PROVIDERS,
    REPOSITORY_PLAN_VERSIONS,
    REPOSITORY_PROVIDER_ORDER,
    RepositoryCheckPlan,
    RepositoryCheckResult,
    RepositoryCheckStatus,
    RepositoryProvider,
)
from yaga.sizes.checker import check_sizes
from yaga.sizes.models import BlobEntry, SizePolicy, SizeSelection
from yaga.sizes.service import CheckedSize
from yaga.trees.checker import check_tree
from yaga.trees.models import TreePolicy, TreeReport, TreeSelection
from yaga.trees.service import CheckedTree


def test_repository_plan_versions_and_provider_orders_are_explicit() -> None:
    assert REPOSITORY_PLAN_VERSIONS == frozenset({1, 2})
    assert REPOSITORY_PLAN_V1_PROVIDER_ORDER == (
        RepositoryProvider.COMMIT,
        RepositoryProvider.WORKFLOW,
        RepositoryProvider.WORKFLOW_SECURITY,
        RepositoryProvider.WORKFLOW_LINT,
    )
    expected_v2_order = (
        RepositoryProvider.COMMIT,
        RepositoryProvider.WORKFLOW,
        RepositoryProvider.WORKFLOW_SECURITY,
        RepositoryProvider.WORKFLOW_LINT,
        RepositoryProvider.MODE,
        RepositoryProvider.PATH,
        RepositoryProvider.SIZE,
        RepositoryProvider.TREE,
    )
    assert REPOSITORY_PROVIDER_ORDER == expected_v2_order
    assert REPOSITORY_PLAN_V2_PROVIDER_ORDER == expected_v2_order
    assert REPOSITORY_PLAN_V1_PROVIDERS == frozenset(REPOSITORY_PLAN_V1_PROVIDER_ORDER)
    assert REPOSITORY_PLAN_V2_PROVIDERS == frozenset(REPOSITORY_PLAN_V2_PROVIDER_ORDER)


def test_repository_plan_v2_requires_and_preserves_matching_policy_paths() -> None:
    plan = RepositoryCheckPlan(
        plan_version=2,
        checks=(
            RepositoryProvider.MODE,
            RepositoryProvider.PATH,
            RepositoryProvider.SIZE,
            RepositoryProvider.TREE,
        ),
        mode_policy_path=PurePosixPath(".yaga/mode.toml"),
        path_policy_path=PurePosixPath(".yaga/path.toml"),
        size_policy_path=PurePosixPath(".yaga/size.toml"),
        tree_policy_path=PurePosixPath(".yaga/tree.toml"),
    )

    assert plan.mode_policy_path == PurePosixPath(".yaga/mode.toml")
    assert plan.path_policy_path == PurePosixPath(".yaga/path.toml")
    assert plan.size_policy_path == PurePosixPath(".yaga/size.toml")
    assert plan.tree_policy_path == PurePosixPath(".yaga/tree.toml")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RepositoryCheckPlan(True, (RepositoryProvider.COMMIT,)),
        lambda: RepositoryCheckPlan(1, (RepositoryProvider.MODE,)),
        lambda: RepositoryCheckPlan(2, (RepositoryProvider.MODE,)),
        lambda: RepositoryCheckPlan(
            2,
            (RepositoryProvider.COMMIT,),
            mode_policy_path=PurePosixPath(".yaga/mode.toml"),
        ),
        lambda: RepositoryCheckPlan(
            2,
            (RepositoryProvider.MODE, RepositoryProvider.PATH),
            mode_policy_path=PurePosixPath(".yaga/POLICY.toml"),
            path_policy_path=PurePosixPath(".yaga/policy.toml"),
        ),
    ],
)
def test_repository_plan_model_rejects_version_or_policy_path_mismatches(
    factory: Callable[[], RepositoryCheckPlan],
) -> None:
    with pytest.raises(ValueError, match="repository check plan"):
        factory()


def _mode_checked(tmp_path: Path, *, valid: bool) -> CheckedMode:
    policy = ModePolicy(1, (ModeKind.REGULAR,))
    mode = "100644" if valid else "100755"
    selection = ModeSelection(
        tmp_path.resolve(),
        "candidate",
        "a" * 40,
        "b" * 40,
        (ModeEntry("entry", "c" * 40, mode, "blob"),),
    )
    return CheckedMode(check_modes(policy, selection), (tmp_path / "mode.toml").resolve())


def _path_checked(tmp_path: Path, *, valid: bool) -> CheckedPath:
    policy = PathPolicy(1, ("windows-reserved",))
    selected_path = "README.md" if valid else "NUL.txt"
    selection = PathSelection(
        tmp_path.resolve(),
        "candidate",
        "a" * 40,
        "b" * 40,
        (selected_path,),
    )
    return CheckedPath(check_paths(policy, selection), (tmp_path / "path.toml").resolve())


def _size_checked(tmp_path: Path, *, valid: bool) -> CheckedSize:
    policy = SizePolicy(1, 10)
    size = 5 if valid else 15
    selection = SizeSelection(
        tmp_path.resolve(),
        "candidate",
        "a" * 40,
        "b" * 40,
        (BlobEntry("entry", "c" * 40, "100644", size),),
        (),
    )
    return CheckedSize(check_sizes(policy, selection), (tmp_path / "size.toml").resolve())


def _tree_checked(tmp_path: Path, *, valid: bool) -> CheckedTree:
    policy = TreePolicy(1, ("README.md",), ())
    selection = TreeSelection(
        tmp_path.resolve(),
        "candidate",
        "a" * 40,
        "b" * 40,
        ("README.md",) if valid else (),
    )
    report = check_tree(policy, selection) if not valid else TreeReport(policy, selection, ())
    return CheckedTree(report, (tmp_path / "tree.toml").resolve())


@pytest.mark.parametrize(
    ("provider", "factory"),
    [
        (RepositoryProvider.MODE, _mode_checked),
        (RepositoryProvider.PATH, _path_checked),
        (RepositoryProvider.SIZE, _size_checked),
        (RepositoryProvider.TREE, _tree_checked),
    ],
)
@pytest.mark.parametrize(
    ("valid", "expected"),
    [
        (True, RepositoryCheckStatus.PASSED),
        (False, RepositoryCheckStatus.FAILED),
    ],
)
def test_repository_check_result_reads_nested_policy_report_status(
    tmp_path: Path,
    provider: RepositoryProvider,
    factory: Callable[..., CheckedMode | CheckedPath | CheckedSize | CheckedTree],
    valid: bool,
    expected: RepositoryCheckStatus,
) -> None:
    checked = factory(tmp_path, valid=valid)

    assert RepositoryCheckResult(provider=provider, report=checked).status is expected
