"""Bounded, non-regex matching for repository-relative change patterns."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from yaga.changes.models import _unsafe_character, _validate_changed_path

MAX_CHANGE_PATTERN_BYTES = 512
MAX_CHANGE_PATTERN_COMPONENTS = 64


@dataclass(frozen=True, slots=True)
class _CompiledChangeComponent:
    """One validated path-pattern component compiled for bounded matching."""

    globstar: bool
    literal: str | None
    segments: tuple[str, ...]
    leading_star: bool
    trailing_star: bool


def validate_change_pattern(pattern: str) -> tuple[str, ...]:
    """Validate one closed change-pattern value and return its components."""
    if not isinstance(pattern, str) or not pattern or "\\" in pattern:
        raise ValueError("change pattern must be a canonical repository-relative POSIX pattern")
    try:
        encoded = pattern.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("change pattern must be valid UTF-8") from error

    path = PurePosixPath(pattern)
    components = path.parts
    if (
        not components
        or len(encoded) > MAX_CHANGE_PATTERN_BYTES
        or len(components) > MAX_CHANGE_PATTERN_COMPONENTS
        or path.is_absolute()
        or bool(PureWindowsPath(pattern).drive)
        or path.as_posix() != pattern
        or any(component in {"", ".", ".."} for component in components)
        or any(_unsafe_character(character) for character in pattern)
    ):
        raise ValueError("change pattern must be a canonical repository-relative POSIX pattern")
    if any("**" in component and component != "**" for component in components):
        raise ValueError("change pattern ** wildcard must occupy an entire path component")
    return components


def match_change_pattern(pattern: str, path: str) -> bool:
    """Return whether one canonical changed path fully matches a validated glob."""
    pattern_components = validate_change_pattern(pattern)
    path_components = _validate_changed_path(path)
    compiled_pattern = _compile_change_components(pattern_components)
    return _match_compiled_change_components(compiled_pattern, path_components)


def _compile_change_components(
    pattern_components: tuple[str, ...],
) -> tuple[_CompiledChangeComponent, ...]:
    """Compile validated components once for reuse across changed paths."""
    compiled: list[_CompiledChangeComponent] = []
    for component in pattern_components:
        if component == "**":
            compiled.append(_CompiledChangeComponent(True, None, (), False, False))
        elif "*" not in component:
            compiled.append(_CompiledChangeComponent(False, component, (), False, False))
        else:
            compiled.append(
                _CompiledChangeComponent(
                    False,
                    None,
                    tuple(segment for segment in component.split("*") if segment),
                    component.startswith("*"),
                    component.endswith("*"),
                )
            )
    return tuple(compiled)


def _match_change_components(
    pattern_components: tuple[str, ...],
    path_components: tuple[str, ...],
    *,
    spend_work: Callable[[int], None] | None = None,
) -> bool:
    """Compile and match validated components; retained as a private test seam."""
    return _match_compiled_change_components(
        _compile_change_components(pattern_components),
        path_components,
        spend_work=spend_work,
    )


def _match_compiled_change_components(
    pattern_components: tuple[_CompiledChangeComponent, ...],
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
    pattern: _CompiledChangeComponent,
    value: str,
    *,
    spend_work: Callable[[int], None] | None,
) -> bool:
    """Match one component using anchored, ordered literal segments."""
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
    """Compare one literal at a fixed location and charge actual comparisons."""
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
