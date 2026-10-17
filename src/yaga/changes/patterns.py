"""Change-policy adapter for bounded repository-relative path patterns."""

from __future__ import annotations

from collections.abc import Callable

from yaga import path_patterns
from yaga.changes.models import _validate_changed_path

MAX_CHANGE_PATTERN_BYTES = path_patterns.MAX_PATTERN_BYTES
MAX_CHANGE_PATTERN_COMPONENTS = path_patterns.MAX_PATTERN_COMPONENTS
_CompiledChangeComponent = path_patterns._CompiledComponent
_compile_change_components = path_patterns._compile_components
_match_component = path_patterns._match_component
_literal_at = path_patterns._literal_at
_spend_search_work = path_patterns._spend_search_work
_unsafe_character = path_patterns._unsafe_character


def validate_change_pattern(pattern: str) -> tuple[str, ...]:
    """Validate a pattern while preserving change-policy diagnostic wording."""
    try:
        return path_patterns.validate_pattern(pattern)
    except ValueError as error:
        raise ValueError(str(error).replace("path pattern", "change pattern", 1)) from error


def match_change_pattern(pattern: str, path: str) -> bool:
    """Return whether one canonical changed path fully matches a validated glob."""
    pattern_components = validate_change_pattern(pattern)
    path_components = _validate_changed_path(path)
    compiled_pattern = _compile_change_components(pattern_components)
    return _match_compiled_change_components(compiled_pattern, path_components)


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
    """Preserve the change provider's compiled matcher seam and keyword names."""
    return path_patterns.match_compiled(pattern_components, path_components, spend_work=spend_work)
