"""Bounded, non-regex matching for tracked-tree path patterns."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from yaga.trees.models import (
    MAX_TREE_PATH_COMPONENTS,
    _unsafe_character,
    _validate_tree_path,
)

MAX_TREE_PATTERN_BYTES = 512
MAX_TREE_PATTERN_COMPONENTS = MAX_TREE_PATH_COMPONENTS
MAX_TREE_MATCH_WORK = 10_000_000


class TreeMatchWorkLimitError(RuntimeError):
    """Private-to-the-provider signal that deterministic matcher fuel ran out."""


class _MatchWork:
    """One monotonic matcher-work budget."""

    __slots__ = ("limit", "remaining")

    def __init__(self, limit: int = MAX_TREE_MATCH_WORK) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("tree match-work limit must be a nonnegative integer")
        self.limit = limit
        self.remaining = limit

    def spend(self, units: int) -> None:
        if isinstance(units, bool) or not isinstance(units, int) or units < 0:
            raise AssertionError("tree matcher work cannot be negative")
        if units > self.remaining:
            raise TreeMatchWorkLimitError
        self.remaining -= units


@dataclass(frozen=True, slots=True)
class _CompiledTreeComponent:
    """One validated pattern component compiled for linear matching."""

    globstar: bool
    literal: str | None
    segments: tuple[str, ...]
    leading_star: bool
    trailing_star: bool


def validate_tree_pattern(pattern: str) -> tuple[str, ...]:
    """Validate one closed tree-pattern value and return its components."""
    if not isinstance(pattern, str) or not pattern or "\\" in pattern:
        raise ValueError("tree pattern must be a canonical repository-relative POSIX pattern")
    try:
        encoded = pattern.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("tree pattern must be valid UTF-8") from error

    path = PurePosixPath(pattern)
    components = path.parts
    if (
        len(encoded) > MAX_TREE_PATTERN_BYTES
        or not 1 <= len(components) <= MAX_TREE_PATTERN_COMPONENTS
        or path.is_absolute()
        or bool(PureWindowsPath(pattern).drive)
        or path.as_posix() != pattern
        or any(component in {"", ".", ".."} for component in components)
        or any(_unsafe_character(character) for character in pattern)
    ):
        raise ValueError("tree pattern must be a canonical repository-relative POSIX pattern")
    if any("**" in component and component != "**" for component in components):
        raise ValueError("tree pattern ** wildcard must occupy an entire path component")
    return components


def match_tree_pattern(pattern: str, path: str) -> bool:
    """Return whether one canonical tracked path fully matches a validated pattern."""
    pattern_components = validate_tree_pattern(pattern)
    path_components = _validate_tree_path(path)
    return _match_compiled_tree_components(
        _compile_tree_components(pattern_components),
        path_components,
    )


def _compile_tree_components(
    pattern_components: tuple[str, ...],
) -> tuple[_CompiledTreeComponent, ...]:
    compiled: list[_CompiledTreeComponent] = []
    for component in pattern_components:
        if component == "**":
            compiled.append(_CompiledTreeComponent(True, None, (), False, False))
        elif "*" not in component:
            compiled.append(_CompiledTreeComponent(False, component, (), False, False))
        else:
            compiled.append(
                _CompiledTreeComponent(
                    False,
                    None,
                    tuple(segment for segment in component.split("*") if segment),
                    component.startswith("*"),
                    component.endswith("*"),
                )
            )
    return tuple(compiled)


def _match_tree_components(
    pattern_components: tuple[str, ...],
    path_components: tuple[str, ...],
    *,
    spend_work: Callable[[int], None] | None = None,
) -> bool:
    """Compile and match components; retained as a focused test seam."""
    return _match_compiled_tree_components(
        _compile_tree_components(pattern_components),
        path_components,
        spend_work=spend_work,
    )


def _match_compiled_tree_components(
    pattern_components: tuple[_CompiledTreeComponent, ...],
    path_components: tuple[str, ...],
    *,
    spend_work: Callable[[int], None] | None = None,
) -> bool:
    """Match compiled components with fuel-aware dynamic programming."""
    path_count = len(path_components)
    previous = [False] * (path_count + 1)
    previous[0] = True

    for pattern_component in pattern_components:
        if spend_work is not None:
            spend_work(path_count)
        current = [False] * (path_count + 1)
        if pattern_component.globstar:
            current[0] = previous[0]
            for index in range(1, path_count + 1):
                current[index] = previous[index] or current[index - 1]
        else:
            for index, path_component in enumerate(path_components, start=1):
                current[index] = previous[index - 1] and _match_component(
                    pattern_component,
                    path_component,
                    spend_work=spend_work,
                )
        previous = current
    return previous[path_count]


def _match_component(
    pattern: _CompiledTreeComponent,
    value: str,
    *,
    spend_work: Callable[[int], None] | None,
) -> bool:
    if pattern.literal is not None:
        return _literal_at(pattern.literal, value, 0, spend_work=spend_work, exact=True)

    cursor = 0
    limit = len(value)
    first_middle = 0
    last_middle = len(pattern.segments)

    if not pattern.leading_star and pattern.segments:
        prefix = pattern.segments[0]
        if not _literal_at(prefix, value, 0, spend_work=spend_work):
            return False
        cursor = len(prefix)
        first_middle = 1

    if not pattern.trailing_star and pattern.segments:
        suffix = pattern.segments[-1]
        suffix_start = len(value) - len(suffix)
        if suffix_start < cursor or not _literal_at(
            suffix,
            value,
            suffix_start,
            spend_work=spend_work,
        ):
            return False
        limit = suffix_start
        last_middle -= 1

    for segment in pattern.segments[first_middle:last_middle]:
        found = value.find(segment, cursor, limit)
        if found < 0:
            _spend_search_work(spend_work, max(1, limit - cursor))
            return False
        _spend_search_work(spend_work, max(1, found - cursor + len(segment)))
        cursor = found + len(segment)
    return cursor <= limit


def _literal_at(
    literal: str,
    value: str,
    start: int,
    *,
    spend_work: Callable[[int], None] | None,
    exact: bool = False,
) -> bool:
    if spend_work is not None:
        spend_work(1)
    if start < 0 or start + len(literal) > len(value):
        return False
    if exact and len(literal) != len(value):
        return False
    for offset, character in enumerate(literal):
        if spend_work is not None:
            spend_work(1)
        if value[start + offset] != character:
            return False
    return True


def _spend_search_work(
    spend_work: Callable[[int], None] | None,
    units: int,
) -> None:
    if spend_work is not None:
        spend_work(units)
