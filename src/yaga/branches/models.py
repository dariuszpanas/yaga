"""Immutable models and portable-name invariants for branch policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MAX_BRANCH_NAME_BYTES = 244
MAX_BRANCH_COMPONENTS = 32
MAX_BRANCH_PATTERNS = 64
BRANCH_SYNTAX_CODE = "branch.syntax"
BRANCH_ALLOWED_CODE = "branch.allowed"

_BRANCH_DIAGNOSTIC_CODES = frozenset({BRANCH_SYNTAX_CODE, BRANCH_ALLOWED_CODE})
_ASCII_HEX = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True, slots=True)
class BranchPolicy:
    """One explicit versioned branch-name policy."""

    branch_policy_version: int
    allowed_patterns: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.branch_policy_version, bool)
            or not isinstance(self.branch_policy_version, int)
            or self.branch_policy_version != 1
        ):
            raise ValueError("branch policy version must be 1")
        if (
            type(self.allowed_patterns) is not tuple
            or not self.allowed_patterns
            or len(self.allowed_patterns) > MAX_BRANCH_PATTERNS
            or any(not isinstance(pattern, str) for pattern in self.allowed_patterns)
        ):
            raise ValueError(
                f"branch policy requires 1 through {MAX_BRANCH_PATTERNS} allowed patterns"
            )
        if len(set(self.allowed_patterns)) != len(self.allowed_patterns):
            raise ValueError("branch policy allowed patterns must be unique")

        from yaga.branches.patterns import validate_branch_pattern

        for pattern in self.allowed_patterns:
            validate_branch_pattern(pattern)


@dataclass(frozen=True, slots=True)
class LoadedBranchPolicy:
    """A branch policy together with its explicit resolved source path."""

    policy: BranchPolicy
    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.policy, BranchPolicy):
            raise ValueError("loaded branch policy requires a BranchPolicy")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("loaded branch policy path must be absolute")


@dataclass(frozen=True, slots=True)
class BranchDiagnostic:
    """One stable branch-policy finding."""

    code: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or self.code not in _BRANCH_DIAGNOSTIC_CODES:
            raise ValueError("branch diagnostic code is not recognized")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("branch diagnostic message must be nonempty")


@dataclass(frozen=True, slots=True)
class BranchReport:
    """The deterministic result of checking one explicit branch name."""

    policy: BranchPolicy
    branch: str
    matched_pattern: str | None
    diagnostics: tuple[BranchDiagnostic, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.policy, BranchPolicy):
            raise ValueError("branch report requires a BranchPolicy")
        _validate_branch_transport(self.branch)
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) > 1
            or any(not isinstance(item, BranchDiagnostic) for item in self.diagnostics)
        ):
            raise ValueError("branch report requires at most one diagnostic")

        portable = _is_portable_branch_name(self.branch)
        if self.diagnostics:
            if self.matched_pattern is not None:
                raise ValueError("failed branch report cannot carry a matched pattern")
            code = self.diagnostics[0].code
            if (code == BRANCH_SYNTAX_CODE) == portable:
                raise ValueError("branch report diagnostic does not match branch syntax")
        elif (
            not portable
            or self.matched_pattern is None
            or self.matched_pattern not in self.policy.allowed_patterns
        ):
            raise ValueError("passed branch report requires a configured matched pattern")

    @property
    def status(self) -> str:
        """Return the stable report status."""
        return "failed" if self.diagnostics else "passed"

    @property
    def valid(self) -> bool:
        """Return whether the branch passed policy."""
        return not self.diagnostics


def _validate_branch_transport(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("branch name must be a string")
    if not value:
        raise ValueError("branch name must not be empty")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("branch name must be valid UTF-8") from error
    if len(encoded) > MAX_BRANCH_NAME_BYTES:
        raise ValueError(f"branch name exceeds {MAX_BRANCH_NAME_BYTES} UTF-8 bytes")
    return value


def _is_portable_branch_name(value: str) -> bool:
    """Return whether a transport-safe name follows YAGA's portable subset."""
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False

    components = value.split("/")
    folded = value.casefold()
    if (
        not 1 <= len(components) <= MAX_BRANCH_COMPONENTS
        or any(not component for component in components)
        or ".." in value
        or folded == "head"
        or folded.startswith("refs/")
        or (len(value) in {40, 64} and all(character in _ASCII_HEX for character in value))
    ):
        return False

    for index, component in enumerate(components):
        if any(not _is_branch_literal(character) for character in component):
            return False
        if not _is_ascii_alphanumeric(component[-1]):
            return False
        if index == 0:
            if not _is_ascii_letter(component[0]):
                return False
        elif not _is_ascii_alphanumeric(component[0]):
            return False
        if component.casefold().endswith(".lock"):
            return False
    return True


def _is_ascii_letter(value: str) -> bool:
    return "A" <= value <= "Z" or "a" <= value <= "z"


def _is_ascii_alphanumeric(value: str) -> bool:
    return _is_ascii_letter(value) or "0" <= value <= "9"


def _is_branch_literal(value: str) -> bool:
    return _is_ascii_alphanumeric(value) or value in {".", "_", "-"}
