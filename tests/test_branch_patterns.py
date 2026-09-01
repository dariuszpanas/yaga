"""Tests for portable branch grammar and bounded wildcard matching."""

from __future__ import annotations

from functools import cache
from itertools import product

import pytest

from yaga.branches.models import MAX_BRANCH_COMPONENTS, MAX_BRANCH_NAME_BYTES
from yaga.branches.patterns import match_branch_pattern, validate_branch_pattern


@pytest.mark.parametrize(
    "pattern",
    [
        "*",
        "**",
        "*-fix",
        "x-*",
        "*.x",
        "x.*",
        "x.lock*",
        "**/1*",
        "HEAD",
        "refs/**",
        "a" * 40,
    ],
)
def test_reachable_wildcard_edges_and_unmatchable_name_reservations_are_valid_patterns(
    pattern: str,
) -> None:
    assert validate_branch_pattern(pattern) == tuple(pattern.split("/"))


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "é",
        "a" * (MAX_BRANCH_NAME_BYTES + 1),
        "/feat",
        "feat/",
        "feat//topic",
        "/".join("a" for _ in range(MAX_BRANCH_COMPONENTS + 1)),
        "feat/**x",
        "feat/x**",
        "feat/***",
        "feat/a***b",
        "1*",
        "-feat*",
        "feat/-x",
        "feat/x-",
        "feat/.x*",
        "feat/*x.",
        "feat/a..b",
        "feat/x.lock",
        "feat/*.LOCK",
        "feat/$x",
        "feat/x?",
    ],
)
def test_patterns_reject_nonportable_or_unsupported_syntax(pattern: str) -> None:
    with pytest.raises(ValueError, match="branch pattern"):
        validate_branch_pattern(pattern)


def test_pattern_byte_and_component_limits_are_inclusive() -> None:
    byte_limited = "a" * MAX_BRANCH_NAME_BYTES
    component_limited = "/".join("a" for _ in range(MAX_BRANCH_COMPONENTS))

    assert validate_branch_pattern(byte_limited) == (byte_limited,)
    assert len(validate_branch_pattern(component_limited)) == MAX_BRANCH_COMPONENTS


@pytest.mark.parametrize(
    ("pattern", "branch", "expected"),
    [
        ("main", "main", True),
        ("main", "Main", False),
        ("feat/*", "feat/topic", True),
        ("feat/*", "feat", False),
        ("feat/*", "feat/topic/nested", False),
        ("feat/**", "feat", True),
        ("feat/**", "feat/topic", True),
        ("feat/**", "feat/topic/nested", True),
        ("feat/*/**", "feat", False),
        ("feat/*/**", "feat/topic", True),
        ("feat/*/**", "feat/topic/nested", True),
        ("**/main", "main", True),
        ("**/main", "release/main", True),
        ("**/main", "release/Main", False),
        ("dependabot/*/**", "dependabot/npm_and_yarn/pkg/acorn-6.4.1", True),
        ("dependabot/*/**", "dependabot", False),
        ("**/1*", "feat/123", True),
        ("**/1*", "feat/topic", False),
        ("f*t", "feat", True),
        ("f*t", "fix", False),
        ("a*b*c", "axybzc", True),
        ("a*b*c", "acb", False),
        ("*", "feat", True),
        ("*", "feat/topic", False),
        ("**", "feat/topic", True),
    ],
)
def test_patterns_are_anchored_case_sensitive_and_component_aware(
    pattern: str,
    branch: str,
    expected: bool,
) -> None:
    assert match_branch_pattern(pattern, branch) is expected


@pytest.mark.parametrize(
    "branch",
    ["", "bad name", "é", "1-first", "feat//topic", "a" * 40, "refs/heads/main"],
)
def test_direct_matcher_requires_a_portable_branch_name(branch: str) -> None:
    with pytest.raises(ValueError, match="branch name"):
        match_branch_pattern("**", branch)


def test_small_pattern_space_matches_a_recursive_reference() -> None:
    pattern_parts = ("a", "b", "*", "a*", "*a", "a*b", "**")
    branch_parts = ("a", "b", "aa", "ab", "ba")

    for pattern_count in range(1, 4):
        for raw_pattern in product(pattern_parts, repeat=pattern_count):
            pattern = "/".join(raw_pattern)
            for branch_count in range(1, 4):
                for raw_branch in product(branch_parts, repeat=branch_count):
                    branch = "/".join(raw_branch)
                    assert match_branch_pattern(pattern, branch) is _reference_match(
                        raw_pattern,
                        raw_branch,
                    )


def _reference_match(pattern: tuple[str, ...], branch: tuple[str, ...]) -> bool:
    @cache
    def visit(pattern_index: int, branch_index: int) -> bool:
        if pattern_index == len(pattern):
            return branch_index == len(branch)
        if pattern[pattern_index] == "**":
            return visit(pattern_index + 1, branch_index) or (
                branch_index < len(branch) and visit(pattern_index, branch_index + 1)
            )
        return (
            branch_index < len(branch)
            and _reference_component(pattern[pattern_index], branch[branch_index])
            and visit(pattern_index + 1, branch_index + 1)
        )

    return visit(0, 0)


def _reference_component(pattern: str, value: str) -> bool:
    @cache
    def visit(pattern_index: int, value_index: int) -> bool:
        if pattern_index == len(pattern):
            return value_index == len(value)
        if pattern[pattern_index] == "*":
            return visit(pattern_index + 1, value_index) or (
                value_index < len(value) and visit(pattern_index, value_index + 1)
            )
        return (
            value_index < len(value)
            and pattern[pattern_index] == value[value_index]
            and visit(pattern_index + 1, value_index + 1)
        )

    return visit(0, 0)
