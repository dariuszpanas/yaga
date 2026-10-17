"""Dependency-neutral, bounded matching for repository-relative path patterns.

Patterns are anchored and case-sensitive. Only component-local ``*`` and whole-
component ``**`` have special meaning; every other permitted character is literal.
Callers validate path strings before reusing their components across comparisons and
may supply one shared work counter across all matches in an evaluation.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

MAX_PATTERN_BYTES = 512
MAX_PATTERN_COMPONENTS = 64
MAX_PATH_BYTES = 4_096
MAX_PATH_COMPONENTS = 64


@dataclass(frozen=True, slots=True)
class _CompiledComponent:
    """One validated path-pattern component compiled for bounded matching."""

    globstar: bool
    literal: str | None
    segments: tuple[str, ...]
    leading_star: bool
    trailing_star: bool


type CompiledPattern = tuple[_CompiledComponent, ...]


def validate_pattern(pattern: str) -> tuple[str, ...]:
    """Validate a closed path-pattern value and return its components."""
    components = _validate_path_value(
        pattern,
        label="path pattern",
        kind="pattern",
        max_bytes=MAX_PATTERN_BYTES,
        max_components=MAX_PATTERN_COMPONENTS,
    )
    if any("**" in component and component != "**" for component in components):
        raise ValueError("path pattern ** wildcard must occupy an entire path component")
    return components


def validate_path(value: object) -> tuple[str, ...]:
    """Validate a bounded canonical repository-relative POSIX path."""
    return _validate_path_value(
        value,
        label="path",
        kind="path",
        max_bytes=MAX_PATH_BYTES,
        max_components=MAX_PATH_COMPONENTS,
    )


def _validate_path_value(
    value: object,
    *,
    label: str,
    kind: str,
    max_bytes: int,
    max_components: int,
) -> tuple[str, ...]:
    message = f"{label} must be a canonical repository-relative POSIX {kind}"
    # UTF-8 cannot encode a character in fewer than one byte. Reject obviously
    # oversized direct inputs before scanning or allocating their encoded copy.
    if not isinstance(value, str) or not value or len(value) > max_bytes or "\\" in value:
        raise ValueError(message)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid UTF-8") from error
    path = PurePosixPath(value)
    components = path.parts
    if (
        len(encoded) > max_bytes
        or len(components) > max_components
        or not components
        or path.is_absolute()
        or bool(PureWindowsPath(value).drive)
        or path.as_posix() != value
        or any(component in {"", ".", ".."} for component in components)
        or any(_unsafe_character(character) for character in value)
    ):
        raise ValueError(message)
    return components


def _unsafe_character(value: str) -> bool:
    return unicodedata.category(value) in {"Cc", "Cf", "Cs", "Zl", "Zp"}


def compile_pattern(pattern: str) -> CompiledPattern:
    """Validate and compile one pattern once for reuse across canonical paths."""
    return _compile_components(validate_pattern(pattern))


def _compile_components(pattern_components: tuple[str, ...]) -> CompiledPattern:
    """Compile validated components; kept separate for existing provider adapters."""
    compiled: list[_CompiledComponent] = []
    for component in pattern_components:
        if component == "**":
            compiled.append(_CompiledComponent(True, None, (), False, False))
        elif "*" not in component:
            compiled.append(_CompiledComponent(False, component, (), False, False))
        else:
            compiled.append(
                _CompiledComponent(
                    False,
                    None,
                    tuple(segment for segment in component.split("*") if segment),
                    component.startswith("*"),
                    component.endswith("*"),
                )
            )
    return tuple(compiled)


def match_compiled(
    pattern: CompiledPattern,
    path_components: tuple[str, ...],
    *,
    spend_work: Callable[[int], None] | None = None,
) -> bool:
    """Match a compiled pattern to validated components using optional shared fuel."""
    path_count = len(path_components)
    previous = [False] * (path_count + 1)
    previous[0] = True

    for pattern_component in pattern:
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
    pattern: _CompiledComponent,
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


def _spend_search_work(spend_work: Callable[[int], None] | None, units: int) -> None:
    if spend_work is not None:
        spend_work(units)
