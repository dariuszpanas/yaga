from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from yaga.errors import InputError
from yaga.trees import checker
from yaga.trees.checker import check_tree
from yaga.trees.models import (
    TREE_FORBIDDEN_CODE,
    TREE_REQUIRED_CODE,
    TreePolicy,
    TreeSelection,
)


def _selection(repository: Path, *paths: str) -> TreeSelection:
    return TreeSelection(
        repository=repository.resolve(),
        revision="HEAD",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=tuple(sorted(paths)),
    )


def test_check_tree_passes_exact_required_and_allowed_paths(tmp_path: Path) -> None:
    policy = TreePolicy(1, ("README.md", "fixtures/literal*.txt"), ("vendor/**",))
    selection = _selection(tmp_path, "README.md", "fixtures/literal*.txt", "src/yaga.py")

    report = check_tree(policy, selection)

    assert report.valid
    assert report.status == "passed"
    assert report.diagnostics == ()


def test_check_tree_orders_missing_then_lexical_forbidden_and_keeps_first_match(
    tmp_path: Path,
) -> None:
    policy = TreePolicy(
        1,
        ("pyproject.toml", "README.md"),
        ("**/*.pem", "secrets/**", "vendor/**"),
    )
    selection = _selection(
        tmp_path,
        "README.md",
        "secrets/key.pem",
        "vendor/a.bin",
        "vendor/z.bin",
    )

    report = check_tree(policy, selection)

    assert [(item.code, item.path, item.pattern) for item in report.diagnostics] == [
        (TREE_REQUIRED_CODE, "pyproject.toml", None),
        (TREE_FORBIDDEN_CODE, "secrets/key.pem", "**/*.pem"),
        (TREE_FORBIDDEN_CODE, "vendor/a.bin", "vendor/**"),
        (TREE_FORBIDDEN_CODE, "vendor/z.bin", "vendor/**"),
    ]
    assert report.missing_required == ("pyproject.toml",)
    assert report.forbidden_paths == (
        "secrets/key.pem",
        "vendor/a.bin",
        "vendor/z.bin",
    )


def test_check_tree_reports_every_forbidden_path_once(tmp_path: Path) -> None:
    policy = TreePolicy(1, (), ("**", "vendor/**"))
    selection = _selection(tmp_path, "a", "vendor/b", "z")

    report = check_tree(policy, selection)

    assert report.forbidden_paths == selection.paths
    assert all(item.pattern == "**" for item in report.diagnostics)


def test_check_tree_maps_match_work_exhaustion_to_input_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = TreePolicy(1, (), ("**",))
    selection = _selection(tmp_path, "README.md")
    monkeypatch.setattr(checker, "MAX_TREE_MATCH_WORK", 0)

    with pytest.raises(InputError, match="0-unit match-work limit"):
        check_tree(policy, selection)


def test_check_tree_rejects_wrong_model_types(tmp_path: Path) -> None:
    policy = TreePolicy(1, ("README.md",), ())
    selection = _selection(tmp_path, "README.md")

    with pytest.raises(TypeError, match="TreePolicy"):
        check_tree(cast(Any, object()), selection)
    with pytest.raises(TypeError, match="TreeSelection"):
        check_tree(policy, cast(Any, object()))
