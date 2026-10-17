"""Immutable, fully validated version-one test qualification policy models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from yaga.path_patterns import validate_pattern
from yaga.test_planning.limits import (
    MAX_CAPABILITIES,
    MAX_CELLS,
    MAX_DEPENDENCIES,
    MAX_DISTINCT_CAPABILITIES,
    MAX_DISTINCT_TAGS,
    MAX_NEEDS,
    MAX_PATTERN_LIST,
    MAX_PATTERNS,
    MAX_PROFILES,
    MAX_RULES,
    MAX_SUITES,
    MAX_TAGS,
    MAX_TOKEN_BYTES,
    MAX_VARIANTS,
)

_TOKEN = re.compile(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", flags=re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}", flags=re.ASCII)


class SelectionMode(StrEnum):
    """Whether a profile selects impacted suites or the complete catalog."""

    SELECTIVE = "selective"
    FULL = "full"


class UnknownPathPolicy(StrEnum):
    """Fail-closed behavior for otherwise unclassified changed paths."""

    FULL = "full"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Suite:
    """One named obligation with opaque executor and environment metadata."""

    id: str
    binding: str
    tags: tuple[str, ...] = ()
    variants: tuple[str, ...] = ("default",)
    needs: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_token(self.id, label="suite id")
        _validate_token(self.binding, label="suite binding")
        _validate_tokens(self.tags, label="suite tags", maximum=MAX_TAGS)
        _validate_tokens(self.variants, label="suite variants", maximum=MAX_VARIANTS, minimum=1)
        _validate_tokens(self.needs, label="suite needs", maximum=MAX_NEEDS)
        _validate_tokens(self.requires, label="suite requires", maximum=MAX_CAPABILITIES)
        if self.id in self.needs:
            raise ValueError("suite needs must not include its own id")


@dataclass(frozen=True, slots=True)
class Profile:
    """One explicitly selected mode and its mandatory suite IDs."""

    name: str
    mode: SelectionMode
    always: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_token(self.name, label="profile name")
        if not isinstance(self.mode, SelectionMode):
            raise ValueError("profile mode must be selective or full")
        _validate_tokens(self.always, label="profile always", maximum=MAX_SUITES)


@dataclass(frozen=True, slots=True)
class SelectionRule:
    """An additive path trigger selecting suites and/or catalog tags."""

    id: str
    when_any: tuple[str, ...]
    select: tuple[str, ...] = ()
    select_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_token(self.id, label="rule id")
        _validate_patterns(self.when_any, label="rule when-any", minimum=1)
        _validate_tokens(self.select, label="rule select", maximum=MAX_SUITES)
        _validate_tokens(self.select_tags, label="rule select-tags", maximum=MAX_DISTINCT_TAGS)
        if not self.select and not self.select_tags:
            raise ValueError("rule requires a nonempty combined select and select-tags selection")


@dataclass(frozen=True, slots=True)
class QualificationPolicy:
    """A complete closed policy, including validated references and prerequisite DAG."""

    test_policy_version: int
    suites: tuple[Suite, ...]
    profiles: tuple[Profile, ...]
    rules: tuple[SelectionRule, ...] = ()
    unknown_paths: UnknownPathPolicy = UnknownPathPolicy.FULL
    full_on: tuple[str, ...] = ()
    no_impact: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.test_policy_version) is not int or self.test_policy_version != 1:
            raise ValueError("test-policy-version must be the integer 1")
        _validate_models(self.suites, Suite, label="suites", minimum=1, maximum=MAX_SUITES)
        _validate_models(self.profiles, Profile, label="profiles", minimum=1, maximum=MAX_PROFILES)
        _validate_models(self.rules, SelectionRule, label="rules", maximum=MAX_RULES)
        if not isinstance(self.unknown_paths, UnknownPathPolicy):
            raise ValueError("unknown-paths must be full or error")
        _validate_patterns(self.full_on, label="full-on")
        _validate_patterns(self.no_impact, label="no-impact")
        _unique(tuple(suite.id for suite in self.suites), label="suite ids")
        _unique(tuple(profile.name for profile in self.profiles), label="profile names")
        _unique(tuple(rule.id for rule in self.rules), label="rule ids")
        self._validate_totals()
        self._validate_references()

    def _validate_totals(self) -> None:
        if sum(len(suite.variants) for suite in self.suites) > MAX_CELLS:
            raise ValueError(f"catalog exceeds {MAX_CELLS} expanded variant cells")
        if sum(len(suite.needs) for suite in self.suites) > MAX_DEPENDENCIES:
            raise ValueError(f"catalog exceeds {MAX_DEPENDENCIES} prerequisite edges")
        if len({tag for suite in self.suites for tag in suite.tags}) > MAX_DISTINCT_TAGS:
            raise ValueError(f"catalog exceeds {MAX_DISTINCT_TAGS} distinct tags")
        if (
            len({item for suite in self.suites for item in suite.requires})
            > MAX_DISTINCT_CAPABILITIES
        ):
            raise ValueError(f"catalog exceeds {MAX_DISTINCT_CAPABILITIES} distinct capabilities")
        patterns = set(self.full_on) | set(self.no_impact)
        patterns.update(pattern for rule in self.rules for pattern in rule.when_any)
        if len(patterns) > MAX_PATTERNS:
            raise ValueError(f"policy exceeds {MAX_PATTERNS} distinct patterns")

    def _validate_references(self) -> None:
        suite_ids = {suite.id for suite in self.suites}
        tags = {tag for suite in self.suites for tag in suite.tags}
        if any(item not in suite_ids for suite in self.suites for item in suite.needs):
            raise ValueError("suite needs references an unknown suite id")
        if any(item not in suite_ids for profile in self.profiles for item in profile.always):
            raise ValueError("profile always references an unknown suite id")
        if any(item not in suite_ids for rule in self.rules for item in rule.select):
            raise ValueError("rule select references an unknown suite id")
        if any(item not in tags for rule in self.rules for item in rule.select_tags):
            raise ValueError("rule select-tags references an unknown tag with empty expansion")
        remaining = {suite.id: set(suite.needs) for suite in self.suites}
        while remaining:
            ready = {name for name, needs in remaining.items() if not needs}
            if not ready:
                raise ValueError("suite needs contains a prerequisite cycle")
            remaining = {
                name: needs - ready for name, needs in remaining.items() if name not in ready
            }


@dataclass(frozen=True, slots=True)
class LoadedTestPolicy:
    """One explicit policy path and the SHA-256 identity of its exact source bytes."""

    policy: QualificationPolicy
    path: Path
    sha256: str

    def __post_init__(self) -> None:
        if type(self.policy) is not QualificationPolicy:
            raise ValueError("loaded test policy requires a QualificationPolicy")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("loaded test policy path must be absolute")
        if type(self.sha256) is not str or not _DIGEST.fullmatch(self.sha256):
            raise ValueError("loaded test policy sha256 must be a lowercase SHA-256 digest")


def _validate_token(value: object, *, label: str) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= MAX_TOKEN_BYTES
        or not _TOKEN.fullmatch(value)
    ):
        raise ValueError(
            f"{label} must be a 1 through {MAX_TOKEN_BYTES} byte lowercase ASCII token"
        )


def _validate_tokens(value: tuple[str, ...], *, label: str, maximum: int, minimum: int = 0) -> None:
    if type(value) is not tuple or not minimum <= len(value) <= maximum:
        raise ValueError(f"{label} requires {minimum} through {maximum} immutable entries")
    for item in value:
        _validate_token(item, label=label)
    _unique(value, label=label)


def _validate_patterns(value: tuple[str, ...], *, label: str, minimum: int = 0) -> None:
    if type(value) is not tuple or not minimum <= len(value) <= MAX_PATTERN_LIST:
        raise ValueError(
            f"{label} requires {minimum} through {MAX_PATTERN_LIST} immutable patterns"
        )
    for pattern in value:
        validate_pattern(pattern)
    _unique(value, label=label)


def _validate_models[T](
    value: tuple[T, ...], cls: type[T], *, label: str, maximum: int, minimum: int = 0
) -> None:
    if (
        type(value) is not tuple
        or not minimum <= len(value) <= maximum
        or any(type(item) is not cls for item in value)
    ):
        raise ValueError(
            f"{label} requires {minimum} through {maximum} immutable {cls.__name__} entries"
        )


def _unique(value: tuple[str, ...], *, label: str) -> None:
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must be unique")
