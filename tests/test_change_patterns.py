"""Tests for bounded changed-path pattern matching."""

from __future__ import annotations

import pytest

from yaga.changes.models import (
    MAX_CHANGED_PATH_BYTES,
    MAX_CHANGED_PATH_COMPONENTS,
)
from yaga.changes.patterns import (
    MAX_CHANGE_PATTERN_BYTES,
    MAX_CHANGE_PATTERN_COMPONENTS,
    _compile_change_components,
    _match_compiled_change_components,
    match_change_pattern,
    validate_change_pattern,
)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("pyproject.toml", "pyproject.toml", True),
        ("pyproject.toml", "src/pyproject.toml", False),
        ("src/*.py", "src/yaga.py", True),
        ("src/*.py", "src/.py", True),
        ("src/*.py", "src/yaga/models.py", False),
        ("src/**/*.py", "src/yaga.py", True),
        ("src/**/*.py", "src/yaga/models.py", True),
        ("src/**/*.py", "tests/test_yaga.py", False),
        ("**/test_*.py", "test_root.py", True),
        ("**/test_*.py", "tests/unit/test_models.py", True),
        ("docs/**", "docs", True),
        ("docs/**", "docs/guide/index.md", True),
        ("**", "any/depth/file.txt", True),
        ("src/*.PY", "src/yaga.py", False),
        ("$SRC/%literal%/*.py", "$SRC/%literal%/check.py", True),
        ("docs/[guide]?.md", "docs/[guide]?.md", True),
        ("src/caf*.py", "src/café.py", True),
        ("a*a*b", "aaab", True),
        ("a*a*b", "aaaa", False),
    ],
)
def test_closed_patterns_match_complete_paths_case_sensitively(
    pattern: str,
    path: str,
    expected: bool,
) -> None:
    assert match_change_pattern(pattern, path) is expected


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        ".",
        "/src/**/*.py",
        "C:/src/**/*.py",
        "src\\**\\*.py",
        "src//*.py",
        "./src/*.py",
        "src/../tests/*.py",
        "src/",
        "src/**.py",
        "src/a**b.py",
        "src/***.py",
        "src/zero\u200bwidth.py",
        "src/line\nbreak.py",
        "/".join("a" for _ in range(MAX_CHANGE_PATTERN_COMPONENTS + 1)),
        "é" * (MAX_CHANGE_PATTERN_BYTES // 2 + 1),
    ],
)
def test_patterns_reject_noncanonical_unsafe_or_unsupported_values(pattern: str) -> None:
    with pytest.raises(ValueError, match="change pattern"):
        validate_change_pattern(pattern)


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "/src/yaga.py",
        "C:/src/yaga.py",
        "src\\yaga.py",
        "src//yaga.py",
        "./src/yaga.py",
        "src/../yaga.py",
        "src/",
        "src/zero\u200bwidth.py",
        "src/line\nbreak.py",
        "/".join("a" for _ in range(MAX_CHANGED_PATH_COMPONENTS + 1)),
        "é" * (MAX_CHANGED_PATH_BYTES // 2 + 1),
    ],
)
def test_matcher_rejects_noncanonical_changed_paths(path: str) -> None:
    with pytest.raises(ValueError, match="changed path"):
        match_change_pattern("**", path)


def test_pattern_byte_and_component_limits_are_inclusive() -> None:
    byte_limited = "é" * (MAX_CHANGE_PATTERN_BYTES // 2)
    component_limited = "/".join("a" for _ in range(MAX_CHANGE_PATTERN_COMPONENTS))

    assert validate_change_pattern(byte_limited) == (byte_limited,)
    assert len(validate_change_pattern(component_limited)) == MAX_CHANGE_PATTERN_COMPONENTS


def test_double_star_matches_zero_or_more_whole_components_only() -> None:
    assert match_change_pattern("a/**/b/**/c", "a/b/c")
    assert match_change_pattern("a/**/b/**/c", "a/x/y/b/z/c")
    assert not match_change_pattern("a/**/b/**/c", "a/x/b/z/d")


def test_component_star_suffix_mismatch_has_linear_match_work() -> None:
    pattern = "*" + "a" * (MAX_CHANGE_PATTERN_BYTES - 2) + "b"
    value = "a" * MAX_CHANGED_PATH_BYTES
    compiled = _compile_change_components(validate_change_pattern(pattern))
    spent = 0

    def spend_work(units: int) -> None:
        nonlocal spent
        spent += units

    assert not _match_compiled_change_components(
        compiled,
        (value,),
        spend_work=spend_work,
    )
    assert spent <= len(pattern) + 2
