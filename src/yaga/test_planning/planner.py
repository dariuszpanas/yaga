"""Pure, bounded suite selection from an explicit policy and changed paths."""

from __future__ import annotations

import heapq
import json
from dataclasses import asdict, replace

from yaga.errors import ConfigurationError, InputError, safe_error_text
from yaga.path_patterns import compile_pattern, match_compiled, validate_path
from yaga.test_planning.limits import (
    MAX_CHANGED_PATH_BYTES_TOTAL,
    MAX_CHANGED_PATHS,
    MAX_PLAN_BYTES,
    MAX_TOKEN_BYTES,
)
from yaga.test_planning.models import (
    Profile,
    QualificationPolicy,
    SelectionMode,
    Suite,
    UnknownPathPolicy,
)
from yaga.test_planning.selection import SelectedSuite, SelectionPlan, SelectionReason

MAX_MATCH_WORK = 10_000_000
MAX_SELECTION_BYTES = MAX_PLAN_BYTES


class _MatchWork:
    """One monotonic fuel budget across all patterns in a planning call."""

    __slots__ = ("remaining",)

    def __init__(self) -> None:
        self.remaining = MAX_MATCH_WORK

    def spend(self, units: int) -> None:
        if units < 0:
            raise AssertionError("matcher work cannot be negative")
        if units > self.remaining:
            raise ConfigurationError(
                f"test planning exceeds the hard {MAX_MATCH_WORK}-unit match-work limit"
            )
        self.remaining -= units


class _Matches:
    """Compile and match each unique pattern once against the whole input."""

    def __init__(self, paths: tuple[str, ...]) -> None:
        self.paths = tuple((path, tuple(path.split("/"))) for path in paths)
        self.cached: dict[str, frozenset[str]] = {}
        self.work = _MatchWork()

    def combined(self, patterns: tuple[str, ...]) -> set[str]:
        matches: set[str] = set()
        for pattern in sorted(patterns):
            if pattern not in self.cached:
                compiled = compile_pattern(pattern)
                self.cached[pattern] = frozenset(
                    path
                    for path, components in self.paths
                    if match_compiled(compiled, components, spend_work=self.work.spend)
                )
            matches.update(self.cached[pattern])
        return matches


class _Reasons:
    """Bound semantic reasons during expansion, before allocating an entire plan."""

    def __init__(self) -> None:
        self.values: set[SelectionReason] = set()
        self.bytes = 2  # JSON array brackets; the final complete selection has its own bound.

    def add(self, reason: SelectionReason) -> None:
        if reason in self.values:
            return
        size = len(
            json.dumps(
                asdict(reason), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        if self.values:
            size += 1
        if self.bytes + size > MAX_SELECTION_BYTES:
            raise ConfigurationError(
                f"test selection reasons exceed the hard {MAX_SELECTION_BYTES}-byte limit"
            )
        self.bytes += size
        self.values.add(reason)


def resolve_profile(policy: QualificationPolicy, profile: str) -> Profile:
    """Validate one explicit profile name without inspecting source or runner state."""
    if not isinstance(policy, QualificationPolicy):
        raise TypeError("policy must be a QualificationPolicy")
    if type(profile) is not str or not 1 <= len(profile) <= MAX_TOKEN_BYTES:
        raise ConfigurationError(
            f"test profile must be an explicit string of 1 through {MAX_TOKEN_BYTES} characters"
        )
    selected_profile = next((item for item in policy.profiles if item.name == profile), None)
    if selected_profile is None:
        display = safe_error_text(profile, maximum=100)
        raise ConfigurationError(f"unknown test profile: {display!r}")
    return selected_profile


def plan_tests(
    policy: QualificationPolicy,
    *,
    profile: str,
    changed_paths: tuple[str, ...] | None,
) -> SelectionPlan:
    """Plan explicit paths without reading Git, environment, files, or runner state.

    ``None`` means no path comparison was supplied and is legal only for a full
    profile. An empty tuple is an explicit complete empty change. Neither form
    discovers or executes tests.
    """
    selected_profile = resolve_profile(policy, profile)
    paths = _validate_paths(changed_paths)
    reasons = _Reasons()

    if selected_profile.mode is SelectionMode.FULL:
        reasons.add(SelectionReason("test.profile-full"))
        return _finish(policy, profile, paths, SelectionMode.FULL, None, reasons)
    if paths is None:
        raise InputError("selective test planning requires explicit changed paths")

    selected = set(selected_profile.always)
    for suite_id in sorted(selected):
        reasons.add(SelectionReason("test.always", suite=suite_id))
    matches = _Matches(paths)
    full_paths = matches.combined(policy.full_on)
    if full_paths:
        for path in sorted(full_paths):
            reasons.add(SelectionReason("test.full-trigger", path=path))
        return _finish(policy, profile, paths, SelectionMode.FULL, None, reasons)

    classified: set[str] = set()
    for rule in sorted(policy.rules, key=lambda item: item.id):
        rule_paths = matches.combined(rule.when_any)
        if not rule_paths:
            continue
        classified.update(rule_paths)
        targets = set(rule.select)
        tags = set(rule.select_tags)
        targets.update(suite.id for suite in policy.suites if tags.intersection(suite.tags))
        selected.update(targets)
        for path in sorted(rule_paths):
            for suite_id in sorted(targets):
                reasons.add(SelectionReason("test.rule", suite=suite_id, rule=rule.id, path=path))

    unclassified = set(paths) - classified
    if unclassified:
        no_impact = matches.combined(policy.no_impact) & unclassified
        for path in sorted(no_impact):
            reasons.add(SelectionReason("test.no-impact", path=path))
        unknown = unclassified - no_impact
        if unknown:
            if policy.unknown_paths is UnknownPathPolicy.ERROR:
                display = safe_error_text(min(unknown), maximum=200)
                raise ConfigurationError(
                    f"test planning has {len(unknown)} unknown changed path(s); first: {display!r}"
                )
            for path in sorted(unknown):
                reasons.add(SelectionReason("test.unknown-path", path=path))
            return _finish(policy, profile, paths, SelectionMode.FULL, None, reasons)

    suites = {suite.id: suite for suite in policy.suites}
    _include_prerequisites(suites, selected, reasons)
    return _finish(policy, profile, paths, SelectionMode.SELECTIVE, selected, reasons)


def _validate_paths(value: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if type(value) is not tuple or len(value) > MAX_CHANGED_PATHS:
        raise InputError(f"changed paths must be an explicit tuple of at most {MAX_CHANGED_PATHS}")
    total_bytes = 0
    for path in value:
        try:
            validate_path(path)
        except ValueError as error:
            detail = safe_error_text(error, maximum=200)
            raise InputError(f"invalid changed path for test planning: {detail}") from error
        total_bytes += len(path.encode("utf-8")) + 1
        if total_bytes > MAX_CHANGED_PATH_BYTES_TOTAL:
            raise InputError(
                f"changed paths exceed the hard {MAX_CHANGED_PATH_BYTES_TOTAL}-byte limit"
            )
    if len(set(value)) != len(value):
        raise InputError("changed paths must be unique")
    return tuple(sorted(value))


def _include_prerequisites(suites: dict[str, Suite], selected: set[str], reasons: _Reasons) -> None:
    remaining = list(selected)
    heapq.heapify(remaining)
    visited: set[str] = set()
    while remaining:
        suite_id = heapq.heappop(remaining)
        if suite_id in visited:
            continue
        visited.add(suite_id)
        for prerequisite in sorted(suites[suite_id].needs):
            reasons.add(
                SelectionReason("test.prerequisite", suite=prerequisite, required_by=suite_id)
            )
            if prerequisite not in selected:
                selected.add(prerequisite)
                heapq.heappush(remaining, prerequisite)


def _topological_order(suites: dict[str, Suite], selected: set[str]) -> tuple[str, ...]:
    dependencies = {suite_id: len(suites[suite_id].needs) for suite_id in selected}
    dependents: dict[str, list[str]] = {suite_id: [] for suite_id in selected}
    for suite_id in selected:
        for prerequisite in suites[suite_id].needs:
            dependents[prerequisite].append(suite_id)
    ready = [suite_id for suite_id, count in dependencies.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        suite_id = heapq.heappop(ready)
        ordered.append(suite_id)
        for dependent in dependents[suite_id]:
            dependencies[dependent] -= 1
            if dependencies[dependent] == 0:
                heapq.heappush(ready, dependent)
    return tuple(ordered)


def _finish(
    policy: QualificationPolicy,
    profile: str,
    paths: tuple[str, ...] | None,
    mode: SelectionMode,
    selected: set[str] | None,
    reasons: _Reasons,
) -> SelectionPlan:
    inventory = tuple(
        replace(
            suite,
            tags=tuple(sorted(suite.tags)),
            variants=tuple(sorted(suite.variants)),
            needs=tuple(sorted(suite.needs)),
            requires=tuple(sorted(suite.requires)),
        )
        for suite in sorted(policy.suites, key=lambda item: item.id)
    )
    suites = {suite.id: suite for suite in inventory}
    selected_ids = set(suites) if selected is None else selected
    result = SelectionPlan(
        profile=profile,
        requested_mode=next(item.mode for item in policy.profiles if item.name == profile),
        effective_mode=mode,
        changed_paths=paths,
        inventory=inventory,
        selected=tuple(
            SelectedSuite(suite_id, suites[suite_id].variants)
            for suite_id in _topological_order(suites, selected_ids)
        ),
        reasons=tuple(sorted(reasons.values, key=SelectionReason.sort_key)),
    )
    _check_selection_size(result)
    return result


def _check_selection_size(plan: SelectionPlan) -> None:
    """Bound the semantic selection without exposing a phase-two wire format."""
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    remaining = MAX_SELECTION_BYTES
    for chunk in encoder.iterencode(asdict(plan)):
        remaining -= len(chunk.encode("utf-8"))
        if remaining < 0:
            raise ConfigurationError(
                f"test selection exceeds the hard {MAX_SELECTION_BYTES}-byte limit"
            )
