from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yaga.paths.models import (
    ASCII_CASE_COLLISION_RULE,
    MAX_PATH_DIAGNOSTICS,
    MAX_PATH_ENTRIES,
    PATH_ASCII_CASE_COLLISION_CODE,
    PATH_WINDOWS_CHARACTER_CODE,
    PATH_WINDOWS_RESERVED_CODE,
    PATH_WINDOWS_TRAILING_CODE,
    WINDOWS_CHARACTERS_RULE,
    WINDOWS_COMPATIBLE_V1_PROFILE,
    WINDOWS_RESERVED_RULE,
    WINDOWS_TRAILING_RULE,
    PathDiagnostic,
    PathPolicy,
    PathReport,
    PathRuleCount,
    PathSelection,
    diagnostic_code_for_rule,
)

OID = "a" * 40
TREE_OID = "b" * 40


def _selection(tmp_path: Path, *paths: str, revision: str = "HEAD") -> PathSelection:
    return PathSelection(
        repository=tmp_path.resolve(),
        revision=revision,
        commit_sha=OID,
        tree_sha=TREE_OID,
        paths=tuple(sorted(paths)),
    )


def test_path_policy_normalizes_custom_rules_to_fixed_order() -> None:
    policy = PathPolicy(
        path_policy_version=1,
        rules=(ASCII_CASE_COLLISION_RULE, WINDOWS_CHARACTERS_RULE),
    )

    assert policy.rules == (WINDOWS_CHARACTERS_RULE, ASCII_CASE_COLLISION_RULE)
    assert policy.profile is None


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"path_policy_version": True, "rules": (WINDOWS_CHARACTERS_RULE,)}, "version"),
        ({"path_policy_version": 1, "rules": ()}, "1 through 4"),
        (
            {
                "path_policy_version": 1,
                "rules": (WINDOWS_CHARACTERS_RULE, WINDOWS_CHARACTERS_RULE),
            },
            "unique",
        ),
        ({"path_policy_version": 1, "rules": ("unknown",)}, "not recognized"),
        ({"path_policy_version": 1, "rules": [WINDOWS_CHARACTERS_RULE]}, "tuple"),
        (
            {
                "path_policy_version": 1,
                "rules": (WINDOWS_CHARACTERS_RULE,),
                "profile": WINDOWS_COMPATIBLE_V1_PROFILE,
            },
            "complete fixed rule set",
        ),
    ],
)
def test_path_policy_rejects_invalid_model_values(
    kwargs: dict[str, Any],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        PathPolicy(**kwargs)


def test_path_selection_preserves_git_valid_portability_findings(tmp_path: Path) -> None:
    paths = ("aux.txt", "colon:name", "control\x01name", "dir\\name", "trailing. ")

    selection = _selection(tmp_path, *paths)

    assert selection.paths == tuple(sorted(paths))


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/absolute",
        "./file",
        "dir/../file",
        "dir//file",
        "dir/file/",
        "nul\x00byte",
        "surrogate\ud800",
        "é" * 2_049,
        "/".join("x" for _ in range(65)),
    ],
)
def test_path_selection_rejects_noncanonical_or_unrepresentable_paths(
    tmp_path: Path,
    path: str,
) -> None:
    with pytest.raises(ValueError, match="committed path"):
        _selection(tmp_path, path)


@pytest.mark.parametrize(
    "revision",
    [
        "",
        "-HEAD",
        "^HEAD",
        "main..HEAD",
        "main...HEAD",
        "HEAD^@",
        "HEAD^!",
        "HEAD^-",
        "HEAD\n",
        "HEAD\u202e",
        "x" * 513,
    ],
)
def test_path_selection_rejects_unsafe_or_set_revisions(
    tmp_path: Path,
    revision: str,
) -> None:
    with pytest.raises(ValueError, match="path revision"):
        _selection(tmp_path, "README.md", revision=revision)


def test_path_selection_enforces_identity_order_and_leaf_topology(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="repository"):
        PathSelection(Path("."), "HEAD", OID, TREE_OID, ())
    with pytest.raises(ValueError, match="object IDs"):
        PathSelection(tmp_path.resolve(), "HEAD", OID.upper(), TREE_OID, ())
    with pytest.raises(ValueError, match="unique and sorted"):
        PathSelection(tmp_path.resolve(), "HEAD", OID, TREE_OID, ("z", "a"))
    with pytest.raises(ValueError, match="unique and sorted"):
        PathSelection(tmp_path.resolve(), "HEAD", OID, TREE_OID, ("a", "a"))
    with pytest.raises(ValueError, match="cannot contain"):
        PathSelection(tmp_path.resolve(), "HEAD", OID, TREE_OID, ("a", "a/b"))


def test_path_diagnostics_enforce_closed_rule_specific_shape() -> None:
    character = PathDiagnostic(
        PATH_WINDOWS_CHARACTER_CODE,
        "contains U+003A",
        "a:b",
        component=1,
    )
    collision = PathDiagnostic(
        PATH_ASCII_CASE_COLLISION_CODE,
        "collides",
        "Readme.md",
        related_path="README.md",
    )

    assert character.rule == WINDOWS_CHARACTERS_RULE
    assert collision.rule == ASCII_CASE_COLLISION_RULE
    assert diagnostic_code_for_rule(WINDOWS_RESERVED_RULE) == PATH_WINDOWS_RESERVED_CODE
    prefix_collision = PathDiagnostic(
        PATH_ASCII_CASE_COLLISION_CODE,
        "file conflicts with directory",
        "A",
        related_path="a/child",
    )
    assert prefix_collision.related_path == "a/child"
    with pytest.raises(ValueError, match="not recognized"):
        PathDiagnostic("path.unknown", "message", "path", component=1)
    with pytest.raises(ValueError, match="component"):
        PathDiagnostic(PATH_WINDOWS_TRAILING_CODE, "message", "path")
    with pytest.raises(ValueError, match="related path"):
        PathDiagnostic(
            PATH_ASCII_CASE_COLLISION_CODE,
            "message",
            "a",
            related_path="b",
        )
    with pytest.raises(ValueError, match="related path"):
        PathDiagnostic(
            PATH_ASCII_CASE_COLLISION_CODE,
            "harmless directory case alias",
            "A/one",
            related_path="a/two",
        )


def test_path_report_preserves_exact_counts_and_bounded_prefix(tmp_path: Path) -> None:
    paths = tuple(f"bad:{index:04d}" for index in range(MAX_PATH_DIAGNOSTICS + 1))
    policy = PathPolicy(1, (WINDOWS_CHARACTERS_RULE,))
    selection = _selection(tmp_path, *paths)
    diagnostics = tuple(
        PathDiagnostic(
            PATH_WINDOWS_CHARACTER_CODE,
            "contains U+003A",
            path,
            component=1,
        )
        for path in selection.paths[:MAX_PATH_DIAGNOSTICS]
    )

    report = PathReport(
        policy=policy,
        selection=selection,
        diagnostics=diagnostics,
        rule_counts=(PathRuleCount(WINDOWS_CHARACTERS_RULE, len(paths)),),
    )

    assert report.status == "failed"
    assert not report.valid
    assert report.finding_count == MAX_PATH_DIAGNOSTICS + 1
    assert report.findings_by_rule == {WINDOWS_CHARACTERS_RULE: MAX_PATH_DIAGNOSTICS + 1}
    assert report.diagnostics_omitted == 1


def test_path_report_requires_path_then_fixed_rule_order_and_exact_small_counts(
    tmp_path: Path,
) -> None:
    policy = PathPolicy(
        1,
        (WINDOWS_CHARACTERS_RULE, WINDOWS_TRAILING_RULE, WINDOWS_RESERVED_RULE),
    )
    selection = _selection(tmp_path, "AUX.", "bad:name")
    ordered = (
        PathDiagnostic(PATH_WINDOWS_TRAILING_CODE, "trailing", "AUX.", component=1),
        PathDiagnostic(PATH_WINDOWS_RESERVED_CODE, "reserved", "AUX.", component=1),
        PathDiagnostic(PATH_WINDOWS_CHARACTER_CODE, "character", "bad:name", component=1),
    )
    counts = (
        PathRuleCount(WINDOWS_CHARACTERS_RULE, 1),
        PathRuleCount(WINDOWS_TRAILING_RULE, 1),
        PathRuleCount(WINDOWS_RESERVED_RULE, 1),
    )

    assert PathReport(policy, selection, ordered, counts).finding_count == 3
    with pytest.raises(ValueError, match="canonical path and rule order"):
        PathReport(policy, selection, tuple(reversed(ordered)), counts)
    with pytest.raises(ValueError, match="exact rule counts"):
        PathReport(
            policy,
            selection,
            ordered,
            (
                PathRuleCount(WINDOWS_CHARACTERS_RULE, 2),
                PathRuleCount(WINDOWS_TRAILING_RULE, 1),
                PathRuleCount(WINDOWS_RESERVED_RULE, 0),
            ),
        )


def test_path_report_represents_every_case_collision_leaf_and_selected_relation(
    tmp_path: Path,
) -> None:
    policy = PathPolicy(1, (ASCII_CASE_COLLISION_RULE,))
    selection = _selection(tmp_path, "A", "a/child")
    diagnostics = (
        PathDiagnostic(
            PATH_ASCII_CASE_COLLISION_CODE,
            "file conflicts with directory",
            "A",
            related_path="a/child",
        ),
        PathDiagnostic(
            PATH_ASCII_CASE_COLLISION_CODE,
            "directory conflicts with file",
            "a/child",
            related_path="A",
        ),
    )

    report = PathReport(
        policy,
        selection,
        diagnostics,
        (PathRuleCount(ASCII_CASE_COLLISION_RULE, 2),),
    )

    assert tuple(item.path for item in report.diagnostics) == ("A", "a/child")
    assert tuple(item.related_path for item in report.diagnostics) == ("a/child", "A")

    unselected_relation = (
        PathDiagnostic(
            PATH_ASCII_CASE_COLLISION_CODE,
            "collision",
            "A",
            related_path="a",
        ),
    )
    with pytest.raises(ValueError, match="selected paths"):
        PathReport(
            policy,
            selection,
            unselected_relation,
            (PathRuleCount(ASCII_CASE_COLLISION_RULE, 1),),
        )


def test_path_rule_count_and_report_enforce_bounds(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="integer from"):
        PathRuleCount(WINDOWS_CHARACTERS_RULE, True)
    with pytest.raises(ValueError, match="integer from"):
        PathRuleCount(WINDOWS_CHARACTERS_RULE, MAX_PATH_ENTRIES + 1)

    policy = PathPolicy(1, (WINDOWS_CHARACTERS_RULE,))
    selection = _selection(tmp_path, "clean")
    with pytest.raises(ValueError, match="enabled fixed rule order"):
        PathReport(policy, selection, (), ())


def test_path_report_passes_with_exact_zero_counts(tmp_path: Path) -> None:
    policy = PathPolicy(1, (WINDOWS_CHARACTERS_RULE, ASCII_CASE_COLLISION_RULE))
    report = PathReport(
        policy,
        _selection(tmp_path, "README.md"),
        (),
        (
            PathRuleCount(WINDOWS_CHARACTERS_RULE, 0),
            PathRuleCount(ASCII_CASE_COLLISION_RULE, 0),
        ),
    )

    assert report.status == "passed"
    assert report.valid
    assert report.finding_count == 0
    assert report.diagnostics_omitted == 0
