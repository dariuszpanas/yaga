from __future__ import annotations

from functools import cache
from itertools import product

import pytest

from yaga.trees.patterns import (
    TreeMatchWorkLimitError,
    _compile_tree_components,
    _match_compiled_tree_components,
    _MatchWork,
    match_tree_pattern,
    validate_tree_pattern,
)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("README.md", "README.md", True),
        ("README.md", "readme.md", False),
        ("*.pem", "key.pem", True),
        ("*.pem", "nested/key.pem", False),
        ("assets/*/manifest.json", "assets/a/manifest.json", True),
        ("assets/*/manifest.json", "assets/a/b/manifest.json", False),
        ("vendor/**", "vendor/package.bin", True),
        ("vendor/**", "vendor/deep/package.bin", True),
        ("vendor/**", "vendor", True),
        ("**/generated/*", "generated/out", True),
        ("**/generated/*", "src/generated/out", True),
        ("**/generated/*", "src/generated/deep/out", False),
        ("literal[?].txt", "literal[?].txt", True),
        ("literal[?].txt", "literalx.txt", False),
        ("日本語/*.txt", "日本語/資料.txt", True),
    ],
)
def test_match_tree_pattern(pattern: str, path: str, expected: bool) -> None:
    assert match_tree_pattern(pattern, path) is expected


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "/absolute",
        "C:/drive",
        "dir\\file",
        "./file",
        "dir/../file",
        "dir//file",
        "dir/file/",
        "dir/**suffix",
        "dir/prefix**",
        "dir/***/file",
        "line\nbreak",
        "bidi\u202efile",
        "surrogate\ud800",
        "é" * 257,
        "/".join("x" for _ in range(65)),
    ],
)
def test_validate_tree_pattern_rejects_unsafe_or_ambiguous_values(pattern: str) -> None:
    with pytest.raises(ValueError, match="tree pattern"):
        validate_tree_pattern(pattern)


def test_tree_matcher_enforces_supplied_work_budget() -> None:
    compiled = _compile_tree_components(validate_tree_pattern("**"))
    work = _MatchWork(limit=0)

    with pytest.raises(TreeMatchWorkLimitError):
        _match_compiled_tree_components(compiled, ("path",), spend_work=work.spend)


def test_dynamic_programming_matcher_agrees_with_small_reference_space() -> None:
    component_patterns = ("a", "b", "*", "a*", "*b", "a*b", "**")
    path_values = ("a", "b", "ab", "ba")

    for pattern_size in range(1, 4):
        for pattern_components in product(component_patterns, repeat=pattern_size):
            pattern = "/".join(pattern_components)
            for path_size in range(1, 4):
                for path_components in product(path_values, repeat=path_size):
                    path = "/".join(path_components)
                    assert match_tree_pattern(pattern, path) is _reference_match(
                        pattern_components,
                        path_components,
                    ), (pattern, path)


def _reference_match(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    @cache
    def match_from(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern):
            return path_index == len(path)
        component = pattern[pattern_index]
        if component == "**":
            return match_from(pattern_index + 1, path_index) or (
                path_index < len(path) and match_from(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path)
            and _reference_component(component, path[path_index])
            and match_from(pattern_index + 1, path_index + 1)
        )

    return match_from(0, 0)


def _reference_component(pattern: str, value: str) -> bool:
    @cache
    def match_from(pattern_index: int, value_index: int) -> bool:
        if pattern_index == len(pattern):
            return value_index == len(value)
        if pattern[pattern_index] == "*":
            return match_from(pattern_index + 1, value_index) or (
                value_index < len(value) and match_from(pattern_index, value_index + 1)
            )
        return (
            value_index < len(value)
            and pattern[pattern_index] == value[value_index]
            and match_from(pattern_index + 1, value_index + 1)
        )

    return match_from(0, 0)
