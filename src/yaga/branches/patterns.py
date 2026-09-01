"""Closed, non-regex matching for portable branch-name patterns."""

from __future__ import annotations

from dataclasses import dataclass

from yaga.branches.models import (
    MAX_BRANCH_COMPONENTS,
    MAX_BRANCH_NAME_BYTES,
    _is_ascii_alphanumeric,
    _is_ascii_letter,
    _is_branch_literal,
    _is_portable_branch_name,
    _validate_branch_transport,
)


@dataclass(frozen=True, slots=True)
class _CompiledBranchComponent:
    globstar: bool
    literal: str | None
    segments: tuple[str, ...]
    leading_star: bool
    trailing_star: bool


def validate_branch_pattern(pattern: str) -> tuple[str, ...]:
    """Validate one schema-v1 branch pattern and return its components."""
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("branch pattern must be a nonempty string")
    try:
        encoded = pattern.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("branch pattern must contain only portable ASCII characters") from error

    components = pattern.split("/")
    if len(encoded) > MAX_BRANCH_NAME_BYTES:
        raise ValueError(f"branch pattern exceeds {MAX_BRANCH_NAME_BYTES} bytes")
    if not 1 <= len(components) <= MAX_BRANCH_COMPONENTS or any(
        not component for component in components
    ):
        raise ValueError(
            f"branch pattern requires 1 through {MAX_BRANCH_COMPONENTS} nonempty components"
        )
    if ".." in pattern:
        raise ValueError("branch pattern must not contain '..'")

    for index, component in enumerate(components):
        if component == "**":
            continue
        if "**" in component:
            raise ValueError("branch pattern ** wildcard must occupy an entire component")
        if any(character != "*" and not _is_branch_literal(character) for character in component):
            raise ValueError("branch pattern contains an unsupported literal character")
        if component.casefold().endswith(".lock"):
            raise ValueError("branch pattern component must not end with '.lock'")
        if component[0] != "*":
            valid_start = (
                _is_ascii_letter(component[0])
                if index == 0
                else _is_ascii_alphanumeric(component[0])
            )
            if not valid_start:
                raise ValueError("branch pattern component has an invalid literal start")
        if component[-1] != "*" and not _is_ascii_alphanumeric(component[-1]):
            raise ValueError("branch pattern component has an invalid literal ending")
    return tuple(components)


def match_branch_pattern(pattern: str, branch: str) -> bool:
    """Return whether one portable branch name matches one validated pattern."""
    pattern_components = validate_branch_pattern(pattern)
    _validate_branch_transport(branch)
    if not _is_portable_branch_name(branch):
        raise ValueError("branch name does not use the portable YAGA branch syntax")
    return _match_branch_components(
        _compile_branch_components(pattern_components),
        tuple(branch.split("/")),
    )


def _compile_branch_components(
    components: tuple[str, ...],
) -> tuple[_CompiledBranchComponent, ...]:
    compiled: list[_CompiledBranchComponent] = []
    for component in components:
        if component == "**":
            compiled.append(_CompiledBranchComponent(True, None, (), False, False))
        elif "*" not in component:
            compiled.append(_CompiledBranchComponent(False, component, (), False, False))
        else:
            compiled.append(
                _CompiledBranchComponent(
                    False,
                    None,
                    tuple(segment for segment in component.split("*") if segment),
                    component.startswith("*"),
                    component.endswith("*"),
                )
            )
    return tuple(compiled)


def _match_branch_components(
    pattern: tuple[_CompiledBranchComponent, ...],
    branch: tuple[str, ...],
) -> bool:
    branch_count = len(branch)
    previous = [False] * (branch_count + 1)
    previous[0] = True

    for pattern_component in pattern:
        current = [False] * (branch_count + 1)
        if pattern_component.globstar:
            current[0] = previous[0]
            for index in range(1, branch_count + 1):
                current[index] = previous[index] or current[index - 1]
        else:
            for index, branch_component in enumerate(branch, start=1):
                current[index] = previous[index - 1] and _match_component(
                    pattern_component,
                    branch_component,
                )
        previous = current
    return previous[branch_count]


def _match_component(pattern: _CompiledBranchComponent, value: str) -> bool:
    if pattern.literal is not None:
        return pattern.literal == value

    cursor = 0
    limit = len(value)
    first_middle = 0
    last_middle = len(pattern.segments)

    if not pattern.leading_star and pattern.segments:
        prefix = pattern.segments[0]
        if not value.startswith(prefix):
            return False
        cursor = len(prefix)
        first_middle = 1

    if not pattern.trailing_star and pattern.segments:
        suffix = pattern.segments[-1]
        suffix_start = len(value) - len(suffix)
        if suffix_start < cursor or not value.endswith(suffix):
            return False
        limit = suffix_start
        last_middle -= 1

    for segment in pattern.segments[first_middle:last_middle]:
        found = value.find(segment, cursor, limit)
        if found < 0:
            return False
        cursor = found + len(segment)
    return cursor <= limit
