"""Pure evaluation of changed-path coupling policy."""

from __future__ import annotations

from collections.abc import Mapping

from yaga.changes.models import (
    ChangePolicy,
    ChangeReport,
    ChangeRule,
    ChangeRuleResult,
    ChangeSelection,
)
from yaga.changes.patterns import (
    _compile_change_components,
    _CompiledChangeComponent,
    _match_compiled_change_components,
    validate_change_pattern,
)
from yaga.errors import ConfigurationError

MAX_CHANGE_MATCH_WORK = 10_000_000


class _ChangeMatchWorkLimitError(RuntimeError):
    """Private signal that one evaluation exhausted deterministic matcher fuel."""


class _MatchWork:
    """Shared monotonic work budget for one complete policy evaluation."""

    __slots__ = ("remaining",)

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def spend(self, units: int) -> None:
        if units < 0:
            raise AssertionError("matcher work cannot be negative")
        if units > self.remaining:
            raise _ChangeMatchWorkLimitError
        self.remaining -= units


def check_changed_paths(policy: ChangePolicy, selection: ChangeSelection) -> ChangeReport:
    """Evaluate every configured rule against one canonical changed-path selection."""
    if not isinstance(policy, ChangePolicy):
        raise TypeError("policy must be a ChangePolicy")
    if not isinstance(selection, ChangeSelection):
        raise TypeError("selection must be a ChangeSelection")

    path_components = tuple((path, tuple(path.split("/"))) for path in selection.paths)
    compiled_patterns = _compile_patterns(policy)
    cached_matches: dict[str, tuple[str, ...]] = {}
    match_work = _MatchWork(MAX_CHANGE_MATCH_WORK)

    try:
        results = tuple(
            _check_rule(
                rule,
                selected_paths=selection.paths,
                path_components=path_components,
                compiled_patterns=compiled_patterns,
                cached_matches=cached_matches,
                match_work=match_work,
            )
            for rule in policy.rules
        )
    except _ChangeMatchWorkLimitError as error:
        raise ConfigurationError(
            f"changed-path evaluation exceeds the hard {MAX_CHANGE_MATCH_WORK}-unit "
            "match-work limit"
        ) from error

    return ChangeReport(selection=selection, results=results)


def _compile_patterns(
    policy: ChangePolicy,
) -> dict[str, tuple[_CompiledChangeComponent, ...]]:
    compiled: dict[str, tuple[_CompiledChangeComponent, ...]] = {}
    for rule in policy.rules:
        for pattern in (*rule.when_any, *rule.require_any):
            if pattern not in compiled:
                compiled[pattern] = _compile_change_components(validate_change_pattern(pattern))
    return compiled


def _check_rule(
    rule: ChangeRule,
    *,
    selected_paths: tuple[str, ...],
    path_components: tuple[tuple[str, tuple[str, ...]], ...],
    compiled_patterns: Mapping[str, tuple[_CompiledChangeComponent, ...]],
    cached_matches: dict[str, tuple[str, ...]],
    match_work: _MatchWork,
) -> ChangeRuleResult:
    triggered_paths = _combined_matches(
        rule.when_any,
        selected_paths=selected_paths,
        path_components=path_components,
        compiled_patterns=compiled_patterns,
        cached_matches=cached_matches,
        match_work=match_work,
    )
    required_paths = ()
    if triggered_paths:
        required_paths = _combined_matches(
            rule.require_any,
            selected_paths=selected_paths,
            path_components=path_components,
            compiled_patterns=compiled_patterns,
            cached_matches=cached_matches,
            match_work=match_work,
        )
    return ChangeRuleResult(
        rule=rule,
        triggered_paths=triggered_paths,
        required_paths=required_paths,
    )


def _combined_matches(
    patterns: tuple[str, ...],
    *,
    selected_paths: tuple[str, ...],
    path_components: tuple[tuple[str, tuple[str, ...]], ...],
    compiled_patterns: Mapping[str, tuple[_CompiledChangeComponent, ...]],
    cached_matches: dict[str, tuple[str, ...]],
    match_work: _MatchWork,
) -> tuple[str, ...]:
    matched: set[str] = set()
    for pattern in patterns:
        if pattern not in cached_matches:
            cached_matches[pattern] = _matching_paths(
                path_components,
                compiled_patterns[pattern],
                match_work=match_work,
            )
        matched.update(cached_matches[pattern])
    return tuple(path for path in selected_paths if path in matched)


def _matching_paths(
    paths: tuple[tuple[str, tuple[str, ...]], ...],
    pattern: tuple[_CompiledChangeComponent, ...],
    *,
    match_work: _MatchWork,
) -> tuple[str, ...]:
    return tuple(
        path
        for path, components in paths
        if _match_compiled_change_components(
            pattern,
            components,
            spend_work=match_work.spend,
        )
    )
