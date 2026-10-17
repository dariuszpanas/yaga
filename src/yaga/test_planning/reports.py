"""Validated source-bound planning results, without execution evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass

from yaga.errors import InputError
from yaga.test_planning.limits import MAX_PLAN_BYTES, MAX_SUITES
from yaga.test_planning.models import Profile, QualificationPolicy, SelectionMode, Suite
from yaga.test_planning.selection import SelectedSuite, SelectionPlan, SelectionReason
from yaga.test_planning.source import CollectedTestSource, PlanSource

_DIGEST = re.compile(r"[0-9a-f]{64}", flags=re.ASCII)
_TOKEN = re.compile(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", flags=re.ASCII)
_REASON_FIELDS = {
    "test.always": (True, False, False, False),
    "test.rule": (True, True, True, False),
    "test.profile-full": (False, False, False, False),
    "test.full-trigger": (False, False, True, False),
    "test.unknown-path": (False, False, True, False),
    "test.no-impact": (False, False, True, False),
    "test.prerequisite": (True, False, False, True),
}
# Every encoded reason needs more than 64 bytes, even before its string values.
_MAX_REASON_ENTRIES = MAX_PLAN_BYTES // 64


def validate_bindings_sha256(value: str | None) -> str | None:
    """Validate an optional opaque execution-manifest identity before reading Git."""
    if value is not None and (type(value) is not str or not _DIGEST.fullmatch(value)):
        raise InputError("bindings-sha256 must be a lowercase 64-character SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class PlannedTests:
    """One complete selection tied to exact policy and Git object identities."""

    policy_sha256: str
    bindings_sha256: str | None
    source: PlanSource
    selection: SelectionPlan

    def __post_init__(self) -> None:
        if type(self.policy_sha256) is not str or not _DIGEST.fullmatch(self.policy_sha256):
            raise ValueError("policy_sha256 must be a lowercase SHA-256 digest")
        validate_bindings_sha256(self.bindings_sha256)
        if type(self.source) is not PlanSource:
            raise ValueError("test plan source must be a PlanSource")
        if type(self.selection) is not SelectionPlan:
            raise ValueError("test plan selection must be a SelectionPlan")
        CollectedTestSource(self.source, self.selection.changed_paths)
        _validate_selection(self.selection)
        revision_only = self.source.base_commit is None
        if revision_only and self.selection.requested_mode is not SelectionMode.FULL:
            raise ValueError("revision-only test plans require a full profile")


def _validate_selection(plan: SelectionPlan) -> None:
    if not isinstance(plan.effective_mode, SelectionMode):
        raise ValueError("test plan effective mode must be selective or full")
    # Reuse catalog validation for bounds, references, and acyclic prerequisites.
    QualificationPolicy(1, plan.inventory, (Profile(plan.profile, plan.requested_mode),))
    if plan.requested_mode is SelectionMode.FULL and plan.effective_mode is not SelectionMode.FULL:
        raise ValueError("full profiles must produce a full test plan")
    if tuple(sorted(plan.inventory, key=lambda suite: suite.id)) != plan.inventory:
        raise ValueError("test plan inventory must use canonical suite order")
    for suite in plan.inventory:
        for values in (suite.tags, suite.variants, suite.needs, suite.requires):
            if tuple(sorted(values)) != values:
                raise ValueError("test plan inventory must use canonical metadata order")
    suites = {suite.id: suite for suite in plan.inventory}
    selected = _validate_selected(plan, suites)
    _validate_reasons(plan, suites, selected)


def _validate_selected(plan: SelectionPlan, suites: dict[str, Suite]) -> set[str]:
    if type(plan.selected) is not tuple or len(plan.selected) > MAX_SUITES:
        raise ValueError("test plan selected suites must be a bounded immutable tuple")
    selected: set[str] = set()
    for item in plan.selected:
        if (
            type(item) is not SelectedSuite
            or type(item.suite) is not str
            or item.suite not in suites
            or item.suite in selected
            or type(item.variants) is not tuple
            or item.variants != suites[item.suite].variants
        ):
            raise ValueError(
                "test plan selected suites require unique catalog IDs and all variants"
            )
        selected.add(item.suite)
    if plan.effective_mode is SelectionMode.FULL and selected != set(suites):
        raise ValueError("full test plans must select the entire inventory")
    remaining = set(selected)
    visited: set[str] = set()
    for item in plan.selected:
        ready = [name for name in remaining if set(suites[name].needs) <= visited]
        if not ready or item.suite != min(ready):
            raise ValueError("test plan selected suites require canonical prerequisite order")
        visited.add(item.suite)
        remaining.remove(item.suite)
    return selected


def _validate_reasons(plan: SelectionPlan, suites: dict[str, Suite], selected: set[str]) -> None:
    if type(plan.reasons) is not tuple or len(plan.reasons) > _MAX_REASON_ENTRIES:
        raise ValueError("test plan reasons must be a bounded immutable tuple")
    paths = set(plan.changed_paths or ())
    for reason in plan.reasons:
        _validate_reason(reason, suites, selected, paths)
    if tuple(sorted(set(plan.reasons), key=SelectionReason.sort_key)) != plan.reasons:
        raise ValueError("test plan reasons must be unique and canonically ordered")
    codes = {reason.code for reason in plan.reasons}
    if plan.requested_mode is SelectionMode.FULL:
        if plan.reasons != (SelectionReason("test.profile-full"),):
            raise ValueError("full profiles require exactly their profile-full reason")
    elif "test.profile-full" in codes:
        raise ValueError("selective profiles cannot have a profile-full reason")
    fallback = bool(codes & {"test.full-trigger", "test.unknown-path"})
    if plan.requested_mode is SelectionMode.SELECTIVE and fallback != (
        plan.effective_mode is SelectionMode.FULL
    ):
        raise ValueError("test plan fallback reasons must agree with its effective mode")


def _validate_reason(
    reason: SelectionReason, suites: dict[str, Suite], selected: set[str], paths: set[str]
) -> None:
    if type(reason) is not SelectionReason or type(reason.code) is not str:
        raise ValueError("test plan reasons require SelectionReason entries")
    fields = _REASON_FIELDS.get(reason.code)
    values = (reason.suite, reason.rule, reason.path, reason.required_by)
    if fields is None or any(
        (type(value) is not str if required else value is not None)
        for required, value in zip(fields, values, strict=True)
    ):
        raise ValueError("test plan reason has an unsupported code or inconsistent fields")
    if reason.suite is not None and reason.suite not in selected:
        raise ValueError("test plan reason references an unselected suite")
    if reason.rule is not None and (len(reason.rule) > 64 or not _TOKEN.fullmatch(reason.rule)):
        raise ValueError("test plan reason requires a bounded rule token")
    if reason.path is not None and reason.path not in paths:
        raise ValueError("test plan reason references an unknown changed path")
    if reason.required_by is not None and (
        reason.required_by not in selected or reason.suite not in suites[reason.required_by].needs
    ):
        raise ValueError("test plan prerequisite reason references an unknown selected edge")
