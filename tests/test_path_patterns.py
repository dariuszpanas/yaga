"""Characterization tests for the shared bounded path-pattern boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from yaga.path_patterns import (
    MAX_PATH_BYTES,
    MAX_PATH_COMPONENTS,
    MAX_PATTERN_BYTES,
    MAX_PATTERN_COMPONENTS,
    compile_pattern,
    match_compiled,
    validate_path,
    validate_pattern,
)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("src/*.py", "src/module.py", True),
        ("src/*.py", "other/src/module.py", False),
        ("src/*.py", "src/pkg/module.py", False),
        ("src/*.py", "src/module.py/child", False),
        ("src/*.py", "src/.py", True),
        ("src/*.py", "SRC/module.py", False),
        ("a/**/b/**/c", "a/b/c", True),
        ("a/**/b/**/c", "a/x/y/b/z/c", True),
        ("a/**/b/**/c", "a/x/b/z/d", False),
        ("docs/**", "docs", True),
        ("**", "any/path", True),
        ("$HOME/%literal%/[guide]?.md", "$HOME/%literal%/[guide]?.md", True),
        ("$HOME/%literal%/[guide]?.md", "home/value/guide1.md", False),
        ("src/caf*.py", "src/café.py", True),
        ("é.txt", "e\u0301.txt", False),
        ("a*a*b", "aaab", True),
        ("a*a*b", "aaaa", False),
        ("*a*b*", "prefixa-middleb-suffix", True),
        ("*a*b*", "prefixb-middlea-suffix", False),
        ("aba*aba", "ababa", False),
    ],
)
def test_compiled_matching_preserves_closed_anchored_grammar(
    pattern: str, path: str, expected: bool
) -> None:
    assert match_compiled(compile_pattern(pattern), validate_path(path)) is expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        True,
        b"src/file.py",
        "",
        ".",
        "/src/file.py",
        "C:/src/file.py",
        "C:src/file.py",
        "src\\file.py",
        "src//file.py",
        "./src/file.py",
        "src/../file.py",
        "src/",
        "src/zero\u200bwidth.py",
        "src/line\nbreak.py",
        "src/\u2028.py",
        "src/\u2029.py",
        "src/\ud800.py",
    ],
)
def test_path_and_pattern_validation_reject_noncanonical_or_unsafe_values(value: object) -> None:
    with pytest.raises(ValueError, match="path"):
        validate_path(value)
    with pytest.raises(ValueError, match="path pattern"):
        validate_pattern(cast(str, value))


@pytest.mark.parametrize("value", ["src/**.py", "src/a**b.py", "src/***.py"])
def test_globstar_is_special_only_in_an_entire_pattern_component(value: str) -> None:
    with pytest.raises(ValueError, match=r"\*\* wildcard"):
        compile_pattern(value)
    assert validate_path(value) == tuple(value.split("/"))


def test_pattern_byte_and_component_limits_are_inclusive() -> None:
    byte_limited = "é" * (MAX_PATTERN_BYTES // 2)
    component_limited = "/".join("a" for _ in range(MAX_PATTERN_COMPONENTS))

    assert validate_pattern(byte_limited) == (byte_limited,)
    assert len(validate_pattern(component_limited)) == MAX_PATTERN_COMPONENTS
    with pytest.raises(ValueError, match="canonical"):
        validate_pattern(byte_limited + "a")
    with pytest.raises(ValueError, match="canonical"):
        validate_pattern(component_limited + "/a")


def test_path_byte_and_component_limits_are_inclusive() -> None:
    byte_limited = "é" * (MAX_PATH_BYTES // 2)
    component_limited = "/".join("a" for _ in range(MAX_PATH_COMPONENTS))

    assert validate_path(byte_limited) == (byte_limited,)
    assert len(validate_path(component_limited)) == MAX_PATH_COMPONENTS
    with pytest.raises(ValueError, match="canonical"):
        validate_path(byte_limited + "a")
    with pytest.raises(ValueError, match="canonical"):
        validate_path(component_limited + "/a")


@pytest.mark.parametrize(
    ("validate", "maximum"),
    [(validate_pattern, MAX_PATTERN_BYTES), (validate_path, MAX_PATH_BYTES)],
)
def test_oversized_direct_values_are_rejected_before_encoding(
    validate: Callable[[str], tuple[str, ...]], maximum: int
) -> None:
    class EncodeMustNotRun(str):
        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
            raise AssertionError("oversized input must be rejected before UTF-8 encoding")

    with pytest.raises(ValueError, match="canonical"):
        validate(EncodeMustNotRun("a" * (maximum + 1)))


def test_compiled_pattern_is_immutable_and_reusable() -> None:
    compiled = compile_pattern("src/**/*.py")
    attribute = "literal"

    assert isinstance(compiled, tuple)
    with pytest.raises(FrozenInstanceError):
        setattr(compiled[0], attribute, "tests")
    assert match_compiled(compiled, validate_path("src/a.py"))
    assert not match_compiled(compiled, validate_path("tests/a.py"))
    assert match_compiled(compiled, validate_path("src/pkg/b.py"))


def test_match_work_limit_is_shared_inclusive_and_propagates_exhaustion() -> None:
    compiled = compile_pattern("/".join("**" for _ in range(MAX_PATTERN_COMPONENTS)))
    components = validate_path("/".join("a" for _ in range(MAX_PATH_COMPONENTS)))
    exact_work = MAX_PATTERN_COMPONENTS * MAX_PATH_COMPONENTS
    remaining = exact_work * 2

    class WorkLimitError(Exception):
        pass

    def spend_work(units: int) -> None:
        nonlocal remaining
        assert units >= 0
        if units > remaining:
            raise WorkLimitError
        remaining -= units

    assert match_compiled(compiled, components, spend_work=spend_work)
    assert remaining == exact_work
    assert match_compiled(compiled, components, spend_work=spend_work)
    assert remaining == 0
    with pytest.raises(WorkLimitError):
        match_compiled(compiled, components, spend_work=spend_work)
    remaining = exact_work - 1
    with pytest.raises(WorkLimitError):
        match_compiled(compiled, components, spend_work=spend_work)


def test_wildcard_suffix_mismatch_charges_linear_work() -> None:
    compiled = compile_pattern("*" + "a" * (MAX_PATTERN_BYTES - 2) + "b")
    components = validate_path("a" * MAX_PATH_BYTES)
    spent = 0

    def spend_work(units: int) -> None:
        nonlocal spent
        spent += units

    assert not match_compiled(compiled, components, spend_work=spend_work)
    assert spent <= MAX_PATTERN_BYTES + 2
