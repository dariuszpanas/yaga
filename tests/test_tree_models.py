from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yaga.trees.models import (
    TREE_FORBIDDEN_CODE,
    TREE_REQUIRED_CODE,
    TreeDiagnostic,
    TreePolicy,
    TreeReport,
    TreeSelection,
)

OID = "a" * 40
TREE_OID = "b" * 40


def _selection(repository: Path, *paths: str, revision: str = "HEAD") -> TreeSelection:
    return TreeSelection(
        repository=repository.resolve(),
        revision=revision,
        commit_sha=OID,
        tree_sha=TREE_OID,
        paths=tuple(sorted(paths)),
    )


def test_tree_policy_accepts_literal_star_in_required_path() -> None:
    policy = TreePolicy(
        tree_policy_version=1,
        required_paths=("fixtures/literal*.txt",),
        forbidden_patterns=("vendor/**",),
    )

    assert policy.required_paths == ("fixtures/literal*.txt",)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (
            {
                "tree_policy_version": True,
                "required_paths": ("README.md",),
                "forbidden_patterns": (),
            },
            "version",
        ),
        (
            {
                "tree_policy_version": 1,
                "required_paths": (),
                "forbidden_patterns": (),
            },
            "1 through 128",
        ),
        (
            {
                "tree_policy_version": 1,
                "required_paths": ("README.md", "README.md"),
                "forbidden_patterns": (),
            },
            "unique",
        ),
        (
            {
                "tree_policy_version": 1,
                "required_paths": ["README.md"],
                "forbidden_patterns": (),
            },
            "tuple",
        ),
    ],
)
def test_tree_policy_rejects_invalid_model_values(
    kwargs: dict[str, Any],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        TreePolicy(**kwargs)


def test_tree_policy_rejects_required_path_forbidden_by_first_or_later_pattern() -> None:
    with pytest.raises(ValueError, match="is forbidden by pattern 'secrets/\\*\\*'"):
        TreePolicy(
            tree_policy_version=1,
            required_paths=("secrets/key.pem",),
            forbidden_patterns=("vendor/**", "secrets/**"),
        )


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/absolute",
        "C:/drive",
        "dir\\file",
        "./file",
        "dir/../file",
        "dir//file",
        "dir/file/",
        "line\nbreak",
        "bidi\u202efile",
        "surrogate\ud800",
        "é" * 2_049,
        "/".join("x" for _ in range(65)),
    ],
)
def test_tree_policy_rejects_noncanonical_required_paths(path: str) -> None:
    with pytest.raises(ValueError, match="tree path"):
        TreePolicy(
            tree_policy_version=1,
            required_paths=(path,),
            forbidden_patterns=(),
        )


@pytest.mark.parametrize(
    "revision",
    [
        "",
        "-HEAD",
        ":vendor",
        "HEAD:vendor",
        "^HEAD",
        "main..HEAD",
        "main...HEAD",
        "HEAD^@",
        "HEAD^!",
        "HEAD^-",
        "HEAD^-2",
        "HEAD\n",
        "HEAD\u202e",
        "x" * 513,
    ],
)
def test_tree_selection_rejects_unsafe_or_set_revisions(
    tmp_path: Path,
    revision: str,
) -> None:
    with pytest.raises(ValueError, match="tree revision"):
        _selection(tmp_path, "README.md", revision=revision)


@pytest.mark.parametrize("revision", ["HEAD", "HEAD^", "HEAD^2", "HEAD~3", "main@{1}"])
def test_tree_selection_accepts_one_exact_revision(tmp_path: Path, revision: str) -> None:
    assert _selection(tmp_path, "README.md", revision=revision).revision == revision


def test_tree_selection_enforces_repository_oids_and_sorted_unique_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="repository"):
        TreeSelection(Path("."), "HEAD", OID, TREE_OID, ())
    with pytest.raises(ValueError, match="object IDs"):
        TreeSelection(tmp_path.resolve(), "HEAD", OID.upper(), TREE_OID, ())
    with pytest.raises(ValueError, match="unique and sorted"):
        TreeSelection(tmp_path.resolve(), "HEAD", OID, TREE_OID, ("z", "a"))
    with pytest.raises(ValueError, match="unique and sorted"):
        TreeSelection(tmp_path.resolve(), "HEAD", OID, TREE_OID, ("a", "a"))


def test_tree_report_derives_ordered_path_views(tmp_path: Path) -> None:
    policy = TreePolicy(
        tree_policy_version=1,
        required_paths=("README.md", "pyproject.toml"),
        forbidden_patterns=("*.pem", "vendor/**"),
    )
    selection = _selection(tmp_path, "README.md", "secret.pem", "vendor/package.bin")
    diagnostics = (
        TreeDiagnostic(
            code=TREE_REQUIRED_CODE,
            message="missing",
            path="pyproject.toml",
        ),
        TreeDiagnostic(
            code=TREE_FORBIDDEN_CODE,
            message="forbidden",
            path="secret.pem",
            pattern="*.pem",
        ),
        TreeDiagnostic(
            code=TREE_FORBIDDEN_CODE,
            message="forbidden",
            path="vendor/package.bin",
            pattern="vendor/**",
        ),
    )

    report = TreeReport(policy=policy, selection=selection, diagnostics=diagnostics)

    assert report.status == "failed"
    assert not report.valid
    assert report.missing_required == ("pyproject.toml",)
    assert report.forbidden_paths == ("secret.pem", "vendor/package.bin")


def test_tree_report_rejects_nondeterministic_diagnostic_order(tmp_path: Path) -> None:
    policy = TreePolicy(
        tree_policy_version=1,
        required_paths=("one", "two"),
        forbidden_patterns=("bad/**",),
    )
    selection = _selection(tmp_path, "bad/z")
    reversed_missing = (
        TreeDiagnostic(TREE_REQUIRED_CODE, "missing", "two"),
        TreeDiagnostic(TREE_REQUIRED_CODE, "missing", "one"),
    )
    with pytest.raises(ValueError, match="required policy order"):
        TreeReport(policy, selection, reversed_missing)

    unsorted_forbidden = (
        TreeDiagnostic(TREE_REQUIRED_CODE, "missing", "one"),
        TreeDiagnostic(TREE_REQUIRED_CODE, "missing", "two"),
        TreeDiagnostic(TREE_FORBIDDEN_CODE, "bad", "bad/z", "bad/**"),
        TreeDiagnostic(TREE_FORBIDDEN_CODE, "bad", "bad/a", "bad/**"),
    )
    selection_with_two = _selection(tmp_path, "bad/a", "bad/z")
    with pytest.raises(ValueError, match="unique, selected, and sorted"):
        TreeReport(policy, selection_with_two, unsorted_forbidden)


def test_tree_diagnostic_enforces_code_specific_pattern_shape() -> None:
    with pytest.raises(ValueError, match="not recognized"):
        TreeDiagnostic("tree.other", "message", "path")
    with pytest.raises(ValueError, match="cannot carry"):
        TreeDiagnostic(TREE_REQUIRED_CODE, "message", "path", "**")
    with pytest.raises(ValueError, match="requires a pattern"):
        TreeDiagnostic(TREE_FORBIDDEN_CODE, "message", "path")
