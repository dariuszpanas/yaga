"""Immutable bounded models for pull-request description policy."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

MAX_DESCRIPTION_BYTES = 64 * 1024
MAX_DIAGNOSTICS = 256
MAX_TEMPLATE_HEADINGS = 64
MAX_HEADING_CHARACTERS = 256
MAX_PLACEHOLDERS = 128
MAX_PLACEHOLDER_CHARACTERS = 128

DESCRIPTION_CODES = frozenset(
    {
        "description.required",
        "description.word-count",
        "description.length",
        "description.placeholder",
        "description.unchecked-task",
        "description.markdown",
        "description.heading-level",
        "description.heading-length",
        "description.heading-empty",
        "description.html-heading",
        "description.invisible-character",
        "description.tab",
        "description.escaped-newline",
        "description.backtick",
        "description.section-required",
        "description.section-duplicate",
        "description.section-order",
        "description.section-empty",
        "description.template-unchanged",
        "description.typo",
    }
)


@dataclass(frozen=True, slots=True)
class DescriptionPolicy:
    """One closed version-one description policy, without source discovery."""

    description_policy_version: int = 1
    min_words: int = 1
    max_length: int = MAX_DESCRIPTION_BYTES
    placeholders: tuple[str, ...] = ("TODO", "TBD")
    require_checked_tasks: bool = False
    typos: str = "skip"
    heading_min_level: int = 2
    heading_max_length: int = 120

    def __post_init__(self) -> None:
        if type(self.description_policy_version) is not int or self.description_policy_version != 1:
            raise ValueError("description policy version must be the integer 1")
        if type(self.min_words) is not int or not 0 <= self.min_words <= MAX_DESCRIPTION_BYTES:
            raise ValueError(f"min-words must be an integer from 0 through {MAX_DESCRIPTION_BYTES}")
        if type(self.max_length) is not int or not 1 <= self.max_length <= MAX_DESCRIPTION_BYTES:
            raise ValueError(
                f"max-length must be an integer from 1 through {MAX_DESCRIPTION_BYTES}"
            )
        if self.min_words * 2 - 1 > self.max_length:
            raise ValueError("min-words cannot fit within max-length")
        if type(self.require_checked_tasks) is not bool:
            raise ValueError("require-checked-tasks must be a boolean")
        if not isinstance(self.typos, str) or self.typos not in {"skip", "check"}:
            raise ValueError("typos must be 'skip' or 'check'")
        if type(self.heading_min_level) is not int or not 1 <= self.heading_min_level <= 6:
            raise ValueError("heading-min-level must be an integer from 1 through 6")
        if (
            type(self.heading_max_length) is not int
            or not 1 <= self.heading_max_length <= MAX_HEADING_CHARACTERS
        ):
            raise ValueError("heading-max-length must be an integer from 1 through 256")
        if type(self.placeholders) is not tuple or len(self.placeholders) > MAX_PLACEHOLDERS:
            raise ValueError(f"placeholders must contain at most {MAX_PLACEHOLDERS} strings")
        normalized: set[str] = set()
        for value in self.placeholders:
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > MAX_PLACEHOLDER_CHARACTERS
                or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)
            ):
                raise ValueError(
                    "placeholders must be nonempty trimmed single-line strings of at most 128 characters"
                )
            folded = value.casefold()
            if folded in normalized:
                raise ValueError("placeholders must be unique ignoring case")
            normalized.add(folded)


@dataclass(frozen=True, slots=True)
class LoadedDescriptionPolicy:
    """A description policy with its explicit absolute source path."""

    policy: DescriptionPolicy
    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.policy, DescriptionPolicy):
            raise ValueError("loaded description policy requires a DescriptionPolicy")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("loaded description policy path must be absolute")


@dataclass(frozen=True, slots=True)
class DescriptionDiagnostic:
    """One source-located finding with a stable closed diagnostic code."""

    code: str
    message: str
    line: int = 1
    column: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or self.code not in DESCRIPTION_CODES:
            raise ValueError("description diagnostic code is not recognized")
        if not isinstance(self.message, str) or not self.message or len(self.message) > 2048:
            raise ValueError(
                "description diagnostic message must contain 1 through 2048 characters"
            )
        for value in (self.line, self.column):
            if type(value) is not int or not 1 <= value <= MAX_DESCRIPTION_BYTES + 1:
                raise ValueError("description diagnostic location is outside the input bounds")


@dataclass(frozen=True, slots=True)
class DescriptionReport:
    """Complete counts and a bounded prefix of description diagnostics."""

    policy: DescriptionPolicy
    diagnostics: tuple[DescriptionDiagnostic, ...]
    diagnostic_count: int
    word_count: int
    character_count: int
    template_checked: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.policy, DescriptionPolicy):
            raise ValueError("description report requires a DescriptionPolicy")
        if type(self.diagnostics) is not tuple or any(
            not isinstance(item, DescriptionDiagnostic) for item in self.diagnostics
        ):
            raise ValueError("description report diagnostics must be a tuple")
        if (
            type(self.diagnostic_count) is not int
            or not 0 <= self.diagnostic_count <= MAX_DESCRIPTION_BYTES * 4
            or len(self.diagnostics) != min(self.diagnostic_count, MAX_DIAGNOSTICS)
        ):
            raise ValueError("description report diagnostic counts are inconsistent")
        if any(
            type(value) is not int or not 0 <= value <= MAX_DESCRIPTION_BYTES
            for value in (self.word_count, self.character_count)
        ):
            raise ValueError("description report counts are outside the input bounds")
        if type(self.template_checked) is not bool:
            raise ValueError("template_checked must be a boolean")

    @property
    def valid(self) -> bool:
        """Return whether every requested description rule passed."""
        return self.diagnostic_count == 0

    @property
    def status(self) -> str:
        """Return the stable report status."""
        return "passed" if self.valid else "failed"

    @property
    def omitted_diagnostic_count(self) -> int:
        """Return the exact number of findings beyond the stored prefix."""
        return self.diagnostic_count - len(self.diagnostics)
