"""Immutable models and invariants for committed-path portability policy."""

from __future__ import annotations

import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path

MAX_PATH_POLICY_RULES = 4
MAX_PATH_ENTRIES = 50_000
MAX_PATH_BYTES = 4_096
MAX_PATH_COMPONENTS = 64
MAX_PATH_REVISION_CHARS = 512
MAX_PATH_DIAGNOSTICS = 256
MAX_PATH_FINDINGS = MAX_PATH_ENTRIES * MAX_PATH_POLICY_RULES

WINDOWS_CHARACTERS_RULE = "windows-characters"
WINDOWS_TRAILING_RULE = "windows-trailing"
WINDOWS_RESERVED_RULE = "windows-reserved"
ASCII_CASE_COLLISION_RULE = "ascii-case-collision"
PATH_RULES = (
    WINDOWS_CHARACTERS_RULE,
    WINDOWS_TRAILING_RULE,
    WINDOWS_RESERVED_RULE,
    ASCII_CASE_COLLISION_RULE,
)

WINDOWS_COMPATIBLE_V1_PROFILE = "windows-compatible-v1"
WINDOWS_COMPATIBLE_V1_RULES = PATH_RULES

PATH_WINDOWS_CHARACTER_CODE = "path.windows-character"
PATH_WINDOWS_TRAILING_CODE = "path.windows-trailing"
PATH_WINDOWS_RESERVED_CODE = "path.windows-reserved"
PATH_ASCII_CASE_COLLISION_CODE = "path.ascii-case-collision"

_RULE_TO_CODE = {
    WINDOWS_CHARACTERS_RULE: PATH_WINDOWS_CHARACTER_CODE,
    WINDOWS_TRAILING_RULE: PATH_WINDOWS_TRAILING_CODE,
    WINDOWS_RESERVED_RULE: PATH_WINDOWS_RESERVED_CODE,
    ASCII_CASE_COLLISION_RULE: PATH_ASCII_CASE_COLLISION_CODE,
}
_CODE_TO_RULE = {code: rule for rule, code in _RULE_TO_CODE.items()}
_RULE_ORDER = {rule: index for index, rule in enumerate(PATH_RULES)}
_OBJECT_ID_LENGTHS = frozenset({40, 64})
_LOWER_HEX = frozenset("0123456789abcdef")
_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_LOWER = "abcdefghijklmnopqrstuvwxyz"
_ASCII_LOWER_TRANSLATION = str.maketrans(_ASCII_UPPER, _ASCII_LOWER)


@dataclass(frozen=True, slots=True)
class PathPolicy:
    """One explicit versioned committed-path portability policy."""

    path_policy_version: int
    rules: tuple[str, ...]
    profile: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.path_policy_version, bool)
            or not isinstance(self.path_policy_version, int)
            or self.path_policy_version != 1
        ):
            raise ValueError("path policy version must be 1")
        if (
            type(self.rules) is not tuple
            or not 1 <= len(self.rules) <= MAX_PATH_POLICY_RULES
            or any(not isinstance(rule, str) for rule in self.rules)
        ):
            raise ValueError(
                f"path policy rules must be a tuple of 1 through {MAX_PATH_POLICY_RULES} strings"
            )
        if len(set(self.rules)) != len(self.rules):
            raise ValueError("path policy rules must be unique")
        unknown = tuple(rule for rule in self.rules if rule not in _RULE_TO_CODE)
        if unknown:
            raise ValueError(f"path policy rule is not recognized: {unknown[0]}")

        canonical_rules = tuple(rule for rule in PATH_RULES if rule in self.rules)
        object.__setattr__(self, "rules", canonical_rules)
        if self.profile is not None:
            if self.profile != WINDOWS_COMPATIBLE_V1_PROFILE:
                raise ValueError("path policy profile is not recognized")
            if canonical_rules != WINDOWS_COMPATIBLE_V1_RULES:
                raise ValueError(
                    f"{WINDOWS_COMPATIBLE_V1_PROFILE} requires its complete fixed rule set"
                )


@dataclass(frozen=True, slots=True)
class LoadedPathPolicy:
    """A parsed path policy together with its explicit resolved path."""

    policy: PathPolicy
    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.policy, PathPolicy):
            raise ValueError("loaded path policy requires a PathPolicy")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("loaded path policy path must be absolute")


@dataclass(frozen=True, slots=True)
class PathSelection:
    """One exact committed tree and its canonical tracked leaf paths."""

    repository: Path
    revision: str
    commit_sha: str
    tree_sha: str
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.repository, Path) or not self.repository.is_absolute():
            raise ValueError("path selection repository must be absolute")
        _validate_path_revision(self.revision)
        if (
            not _is_object_id(self.commit_sha)
            or not _is_object_id(self.tree_sha)
            or len(self.commit_sha) != len(self.tree_sha)
        ):
            raise ValueError("path selection requires same-format lowercase object IDs")
        if type(self.paths) is not tuple or len(self.paths) > MAX_PATH_ENTRIES:
            raise ValueError(f"path selection exceeds {MAX_PATH_ENTRIES} paths")
        for path in self.paths:
            _validate_committed_path(path)
        if self.paths != tuple(sorted(self.paths)) or len(set(self.paths)) != len(self.paths):
            raise ValueError("path selection paths must be unique and sorted")
        for path in self.paths:
            prefix = f"{path}/"
            candidate_index = bisect_left(self.paths, prefix)
            if candidate_index < len(self.paths) and self.paths[candidate_index].startswith(prefix):
                raise ValueError("path selection leaf paths cannot contain another leaf path")


@dataclass(frozen=True, slots=True)
class PathDiagnostic:
    """One stable committed-path portability finding."""

    code: str
    message: str
    path: str
    component: int | None = None
    related_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or self.code not in _CODE_TO_RULE:
            raise ValueError("path diagnostic code is not recognized")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("path diagnostic message must be nonempty")
        components = _validate_committed_path(self.path)
        if self.rule == ASCII_CASE_COLLISION_RULE:
            if self.component is not None:
                raise ValueError("case-collision diagnostic cannot carry a component")
            related_path = self.related_path
            if not isinstance(related_path, str):
                raise ValueError("case-collision diagnostic requires a related path")
            _validate_committed_path(related_path)
            if related_path == self.path:
                raise ValueError("case-collision diagnostic requires a distinct related path")
            if not _paths_collide_under_ascii_case(self.path, related_path):
                raise ValueError(
                    "case-collision diagnostic related path must collide under ASCII case"
                )
        else:
            if (
                isinstance(self.component, bool)
                or not isinstance(self.component, int)
                or not 1 <= self.component <= len(components)
            ):
                raise ValueError("path diagnostic component must identify a path component")
            if self.related_path is not None:
                raise ValueError("non-collision diagnostic cannot carry a related path")

    @property
    def rule(self) -> str:
        """Return the configured rule that owns this stable diagnostic code."""
        return _CODE_TO_RULE[self.code]


@dataclass(frozen=True, slots=True)
class PathRuleCount:
    """One exact finding count for one enabled path rule."""

    rule: str
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.rule, str) or self.rule not in _RULE_TO_CODE:
            raise ValueError("path finding-count rule is not recognized")
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or not 0 <= self.count <= MAX_PATH_ENTRIES
        ):
            raise ValueError(
                f"path finding count must be an integer from 0 through {MAX_PATH_ENTRIES}"
            )


@dataclass(frozen=True, slots=True)
class PathReport:
    """The bounded deterministic result of checking one exact committed tree."""

    policy: PathPolicy
    selection: PathSelection
    diagnostics: tuple[PathDiagnostic, ...]
    rule_counts: tuple[PathRuleCount, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.policy, PathPolicy):
            raise ValueError("path report requires a PathPolicy")
        if not isinstance(self.selection, PathSelection):
            raise ValueError("path report requires a PathSelection")
        if (
            type(self.rule_counts) is not tuple
            or any(not isinstance(item, PathRuleCount) for item in self.rule_counts)
            or tuple(item.rule for item in self.rule_counts) != self.policy.rules
        ):
            raise ValueError("path report counts must follow the enabled fixed rule order")
        finding_count = sum(item.count for item in self.rule_counts)
        if not 0 <= finding_count <= MAX_PATH_FINDINGS:
            raise ValueError("path report finding count exceeds its hard bound")
        if (
            type(self.diagnostics) is not tuple
            or any(not isinstance(item, PathDiagnostic) for item in self.diagnostics)
            or len(self.diagnostics) != min(finding_count, MAX_PATH_DIAGNOSTICS)
        ):
            raise ValueError(
                "path report must store exactly the bounded prefix of its exact findings"
            )

        selected = set(self.selection.paths)
        if any(
            diagnostic.rule not in self.policy.rules
            or diagnostic.path not in selected
            or (diagnostic.related_path is not None and diagnostic.related_path not in selected)
            for diagnostic in self.diagnostics
        ):
            raise ValueError(
                "path report diagnostics must identify selected paths and enabled rules"
            )
        diagnostic_keys = tuple(
            (diagnostic.path, _RULE_ORDER[diagnostic.rule]) for diagnostic in self.diagnostics
        )
        if diagnostic_keys != tuple(sorted(diagnostic_keys)) or len(set(diagnostic_keys)) != len(
            diagnostic_keys
        ):
            raise ValueError("path report diagnostics must follow canonical path and rule order")

        exact_counts = {item.rule: item.count for item in self.rule_counts}
        stored_counts = dict.fromkeys(self.policy.rules, 0)
        for diagnostic in self.diagnostics:
            stored_counts[diagnostic.rule] += 1
        if any(stored_counts[rule] > exact_counts[rule] for rule in self.policy.rules):
            raise ValueError("stored path diagnostics cannot exceed exact rule counts")
        if finding_count <= MAX_PATH_DIAGNOSTICS and stored_counts != exact_counts:
            raise ValueError("complete stored path diagnostics must match exact rule counts")

    @property
    def status(self) -> str:
        """Return the stable aggregate report status."""
        return "failed" if self.finding_count else "passed"

    @property
    def valid(self) -> bool:
        """Return whether every committed path satisfies the enabled policy."""
        return self.finding_count == 0

    @property
    def finding_count(self) -> int:
        """Return the exact total finding count, including omitted diagnostics."""
        return sum(item.count for item in self.rule_counts)

    @property
    def findings_by_rule(self) -> dict[str, int]:
        """Return exact finding counts in the enabled fixed rule order."""
        return {item.rule: item.count for item in self.rule_counts}

    @property
    def diagnostics_omitted(self) -> int:
        """Return the exact number of findings omitted from stored diagnostics."""
        return self.finding_count - len(self.diagnostics)


def diagnostic_code_for_rule(rule: str) -> str:
    """Return the stable diagnostic code for one closed path rule."""
    try:
        return _RULE_TO_CODE[rule]
    except (KeyError, TypeError) as error:
        raise ValueError("path policy rule is not recognized") from error


def _validate_path_revision(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_PATH_REVISION_CHARS
        or value.startswith(("-", "^"))
        or ".." in value
        or any(selector in value for selector in ("^@", "^!", "^-"))
        or any(character.isspace() or _unsafe_revision_character(character) for character in value)
    ):
        raise ValueError(
            f"path revision must be a safe non-option string of at most "
            f"{MAX_PATH_REVISION_CHARS} characters"
        )
    return value


def _validate_committed_path(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise ValueError("committed path must be a canonical repository-relative UTF-8 leaf path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("committed path must be valid UTF-8") from error
    components = tuple(value.split("/"))
    if (
        len(encoded) > MAX_PATH_BYTES
        or not 1 <= len(components) <= MAX_PATH_COMPONENTS
        or value.startswith("/")
        or "\x00" in value
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise ValueError("committed path must be a canonical repository-relative UTF-8 leaf path")
    return components


def _is_object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in _OBJECT_ID_LENGTHS
        and all(character in _LOWER_HEX for character in value)
    )


def _ascii_lower(value: str) -> str:
    return value.translate(_ASCII_LOWER_TRANSLATION)


def _paths_collide_under_ascii_case(first: str, second: str) -> bool:
    folded_first = _ascii_lower(first)
    folded_second = _ascii_lower(second)
    return (
        folded_first == folded_second
        or folded_first.startswith(f"{folded_second}/")
        or folded_second.startswith(f"{folded_first}/")
    )


def _unsafe_revision_character(value: str) -> bool:
    return unicodedata.category(value) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
