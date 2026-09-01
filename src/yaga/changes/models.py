"""Immutable models for changed-path coupling policy."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_CHANGE_RULES = 32
MAX_CHANGE_PATTERNS = 16
MAX_CHANGE_RULE_NAME_CHARS = 64
MAX_CHANGED_PATHS = 2_048
MAX_CHANGED_PATH_BYTES = 4_096
MAX_CHANGED_PATH_COMPONENTS = 64
MAX_REVISION_RANGE_CHARS = 512
CHANGE_REQUIRE_ANY_CODE = "change.require_any"

_OBJECT_ID_LENGTHS = frozenset({40, 64})
_LOWER_HEX = frozenset("0123456789abcdef")


class ChangeRuleStatus(StrEnum):
    """Outcome of one changed-path coupling rule."""

    SKIPPED = "skipped"
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ChangeRule:
    """One immutable when-any/require-any coupling rule."""

    name: str
    when_any: tuple[str, ...]
    require_any: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _is_rule_name(self.name):
            raise ValueError("change rule name must be a lowercase ASCII slug")
        _validate_pattern_tuple(self.when_any, label="when-any")
        _validate_pattern_tuple(self.require_any, label="require-any")


@dataclass(frozen=True, slots=True)
class ChangePolicy:
    """One versioned changed-path coupling policy."""

    change_policy_version: int
    rules: tuple[ChangeRule, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.change_policy_version, bool)
            or not isinstance(self.change_policy_version, int)
            or self.change_policy_version != 1
        ):
            raise ValueError("change policy version must be 1")
        if (
            type(self.rules) is not tuple
            or not self.rules
            or len(self.rules) > MAX_CHANGE_RULES
            or any(not isinstance(rule, ChangeRule) for rule in self.rules)
        ):
            raise ValueError(f"change policy requires 1 through {MAX_CHANGE_RULES} rules")
        names = {rule.name for rule in self.rules}
        if len(names) != len(self.rules):
            raise ValueError("change policy rule names must be unique")


@dataclass(frozen=True, slots=True)
class LoadedChangePolicy:
    """A parsed change policy and its explicit source path."""

    policy: ChangePolicy
    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ChangePolicy):
            raise ValueError("loaded change policy requires a ChangePolicy")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("loaded change policy path must be absolute")


@dataclass(frozen=True, slots=True)
class ChangeSelection:
    """One exact Git comparison and its canonical changed paths."""

    revision_range: str
    base_sha: str
    head_sha: str
    comparison_sha: str
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.revision_range, str)
            or not self.revision_range
            or len(self.revision_range) > MAX_REVISION_RANGE_CHARS
            or any(
                character.isspace() or _unsafe_character(character)
                for character in self.revision_range
            )
        ):
            raise ValueError("change selection revision range must be bounded and safe")
        object_ids = (self.base_sha, self.head_sha, self.comparison_sha)
        if (
            any(not _is_object_id(value) for value in object_ids)
            or len({len(value) for value in object_ids}) != 1
        ):
            raise ValueError("change selection requires same-format lowercase object IDs")
        if type(self.paths) is not tuple or len(self.paths) > MAX_CHANGED_PATHS:
            raise ValueError(f"change selection exceeds {MAX_CHANGED_PATHS} paths")
        for path in self.paths:
            _validate_changed_path(path)
        if self.paths != tuple(sorted(self.paths)) or len(set(self.paths)) != len(self.paths):
            raise ValueError("change selection paths must be unique and sorted")


@dataclass(frozen=True, slots=True)
class ChangeRuleResult:
    """Matched changed paths and outcome for one policy rule."""

    rule: ChangeRule
    triggered_paths: tuple[str, ...]
    required_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rule, ChangeRule):
            raise ValueError("change rule result requires a ChangeRule")
        _validate_result_paths(self.triggered_paths, label="triggered")
        _validate_result_paths(self.required_paths, label="required")
        if not self.triggered_paths and self.required_paths:
            raise ValueError("skipped change rule result cannot carry required path matches")

    @property
    def code(self) -> str:
        """Return the stable violation code used when this result fails."""
        return CHANGE_REQUIRE_ANY_CODE

    @property
    def status(self) -> ChangeRuleStatus:
        """Return whether the rule was skipped, satisfied, or violated."""
        if not self.triggered_paths:
            return ChangeRuleStatus.SKIPPED
        if self.required_paths:
            return ChangeRuleStatus.PASSED
        return ChangeRuleStatus.FAILED

    @property
    def valid(self) -> bool:
        """Return whether this rule has no violation."""
        return self.status is not ChangeRuleStatus.FAILED


@dataclass(frozen=True, slots=True)
class ChangeReport:
    """Deterministic aggregate changed-path policy result."""

    selection: ChangeSelection
    results: tuple[ChangeRuleResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.selection, ChangeSelection):
            raise ValueError("change report requires a ChangeSelection")
        if (
            type(self.results) is not tuple
            or not self.results
            or len(self.results) > MAX_CHANGE_RULES
            or any(not isinstance(result, ChangeRuleResult) for result in self.results)
        ):
            raise ValueError("change report requires bounded rule results")
        names = tuple(result.rule.name for result in self.results)
        if len(set(names)) != len(names):
            raise ValueError("change report rule results must be unique")
        selected_paths = set(self.selection.paths)
        if any(
            path not in selected_paths
            for result in self.results
            for path in (*result.triggered_paths, *result.required_paths)
        ):
            raise ValueError("change report matched paths must belong to its selection")

    @property
    def checked(self) -> int:
        """Return the total number of evaluated rules."""
        return len(self.results)

    @property
    def skipped(self) -> int:
        """Return the number of rules whose trigger did not match."""
        return sum(result.status is ChangeRuleStatus.SKIPPED for result in self.results)

    @property
    def passed(self) -> int:
        """Return the number of triggered and satisfied rules."""
        return sum(result.status is ChangeRuleStatus.PASSED for result in self.results)

    @property
    def failed(self) -> int:
        """Return the number of triggered rules missing a required path."""
        return sum(result.status is ChangeRuleStatus.FAILED for result in self.results)

    @property
    def valid(self) -> bool:
        """Return whether every triggered rule was satisfied."""
        return self.failed == 0


def _is_rule_name(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_CHANGE_RULE_NAME_CHARS:
        return False
    if value[0] == "-" or value[-1] == "-" or "--" in value:
        return False
    return all(
        "a" <= character <= "z" or "0" <= character <= "9" or character == "-"
        for character in value
    )


def _validate_pattern_tuple(value: tuple[str, ...], *, label: str) -> None:
    if (
        type(value) is not tuple
        or not value
        or len(value) > MAX_CHANGE_PATTERNS
        or any(not isinstance(pattern, str) for pattern in value)
    ):
        raise ValueError(f"change rule {label} requires 1 through {MAX_CHANGE_PATTERNS} patterns")
    if len(set(value)) != len(value):
        raise ValueError(f"change rule {label} patterns must be unique")
    from yaga.changes.patterns import validate_change_pattern

    for pattern in value:
        validate_change_pattern(pattern)


def _validate_result_paths(value: tuple[str, ...], *, label: str) -> None:
    if type(value) is not tuple or len(value) > MAX_CHANGED_PATHS:
        raise ValueError(f"change rule result {label} paths are invalid")
    for path in value:
        _validate_changed_path(path)
    if value != tuple(sorted(value)) or len(set(value)) != len(value):
        raise ValueError(f"change rule result {label} paths must be unique and sorted")


def _validate_changed_path(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("changed path must be a canonical repository-relative POSIX path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("changed path must be valid UTF-8") from error
    path = PurePosixPath(value)
    components = path.parts
    if (
        len(encoded) > MAX_CHANGED_PATH_BYTES
        or len(components) > MAX_CHANGED_PATH_COMPONENTS
        or not components
        or path.is_absolute()
        or bool(PureWindowsPath(value).drive)
        or path.as_posix() != value
        or any(component in {"", ".", ".."} for component in components)
        or any(_unsafe_character(character) for character in value)
    ):
        raise ValueError("changed path must be a canonical repository-relative POSIX path")
    return components


def _is_object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in _OBJECT_ID_LENGTHS
        and all(character in _LOWER_HEX for character in value)
    )


def _unsafe_character(value: str) -> bool:
    return unicodedata.category(value) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
