"""Immutable models shared by Conventional Commit modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal


class CasePolicy(StrEnum):
    """Configured casing for a type or scope token."""

    ANY = "any"
    LOWER = "lower"
    UPPER = "upper"


class PresencePolicy(StrEnum):
    """Whether an optional Conventional Commit component may appear."""

    OPTIONAL = "optional"
    REQUIRED = "required"
    FORBIDDEN = "forbidden"


class EndingPolicy(StrEnum):
    """Whether a description may end in sentence punctuation."""

    ALLOW = "allow"
    REQUIRE = "require"
    FORBID = "forbid"
    FORBID_PERIOD = "forbid-period"


class DescriptionCasePolicy(StrEnum):
    """Optional initial-letter policy, independent of type and scope casing."""

    ANY = "any"
    FORBID_INITIAL_UPPER = "forbid-initial-uppercase"


class LengthUnit(StrEnum):
    """Unit used for configured text length bounds."""

    CODEPOINTS = "codepoints"
    UTF16 = "utf16"


class FooterSyntax(StrEnum):
    """Closed footer separator grammars, separate from token presence policy."""

    CONVENTIONAL = "conventional"
    COLON_WHITESPACE = "colon-whitespace"


class LineLengthURLPolicy(StrEnum):
    """Whether HTTP references exempt a complete line from wrapping limits."""

    CHECK = "check"
    EXEMPT = "exempt"


class MergePolicy(StrEnum):
    """How range checks handle commits with multiple parents."""

    IGNORE = "ignore"
    CHECK = "check"
    REJECT = "reject"


class BreakingMarkerPolicy(StrEnum):
    """How header and footer breaking-change markers relate."""

    EITHER = "either"
    PAIRED = "paired"


class DependabotPullRequestPolicy(StrEnum):
    """Whether Dependabot pull-request commit policy is checked."""

    CHECK = "check"
    SKIP = "skip"


class PullRequestMessagePolicy(StrEnum):
    """Which proposed pull-request message receives full commit checks."""

    TITLE_ONLY = "title-only"
    TITLE_AND_BODY = "title-and-body"


class TyposPolicy(StrEnum):
    """Whether the optional Typos CLI checks each selected message."""

    SKIP = "skip"
    CHECK = "check"


class TyposConfig(StrEnum):
    """Whether spelling checks discover ambient Typos configuration."""

    REPOSITORY = "repository"
    ISOLATED = "isolated"


class ParagraphSplittingPolicy(StrEnum):
    """Whether to detect likely sentence splits across blank lines."""

    SKIP = "skip"
    CHECK = "check"


class QualityProvider(StrEnum):
    """Optional provider used for advisory commit quality checks."""

    HUGGINGFACE = "huggingface"
    BEDROCK = "bedrock"


class QualityTask(StrEnum):
    """Optional local task used by a quality provider."""

    CLASSIFICATION = "classification"
    SEQ2SEQ = "seq2seq"


class QualityInputMode(StrEnum):
    """Which part of a commit message the optional quality model receives."""

    MESSAGE = "message"
    TITLE = "title"


class MessageFormat(StrEnum):
    """Explicit message structure, independent of prose policy."""

    CONVENTIONAL = "conventional"
    PLAIN = "plain"


class DiagnosticSeverity(StrEnum):
    """Whether a policy finding blocks validation."""

    ERROR = "error"
    WARNING = "warning"


class OutputFormat(StrEnum):
    """Supported stable report formats."""

    TEXT = "text"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    """Non-secret defaults for the optional quality advisory."""

    provider: QualityProvider = QualityProvider.HUGGINGFACE
    task: QualityTask = QualityTask.CLASSIFICATION
    model_id: str = "saridormi/commit-message-quality-codebert"
    revision: str | None = "30c7895b3eb0270a3246ef3db7b43c837d8e553a"
    threshold: float = 0.70
    region: str | None = None
    max_tokens: int = 32
    max_input_tokens: int = 512
    input_mode: QualityInputMode = QualityInputMode.MESSAGE

    @classmethod
    def for_provider(cls, provider: QualityProvider) -> QualityPolicy:
        """Return compatible defaults for one provider's model and task."""
        if provider is QualityProvider.BEDROCK:
            return cls(provider=provider, model_id="amazon.nova-micro-v1:0", revision=None)
        return cls()


@dataclass(frozen=True, slots=True)
class CommitPolicy:
    """Resolved Conventional Commit policy."""

    config_version: int = 1
    allowed_types: tuple[str, ...] | None = None
    type_case: CasePolicy = CasePolicy.ANY
    scope_policy: PresencePolicy = PresencePolicy.OPTIONAL
    allowed_scopes: tuple[str, ...] | None = None
    scope_case: CasePolicy = CasePolicy.ANY
    header_max_length: int | None = None
    description_min_length: int = 1
    description_max_length: int | None = None
    description_ending: EndingPolicy = EndingPolicy.ALLOW
    body_policy: PresencePolicy = PresencePolicy.OPTIONAL
    body_min_length: int = 0
    body_max_line_length: int | None = None
    merge_commits: MergePolicy = MergePolicy.IGNORE
    ignored_headers: tuple[str, ...] = ()
    max_commits: int = 256
    body_min_words: int = 0
    breaking_markers: BreakingMarkerPolicy = BreakingMarkerPolicy.EITHER
    required_footer_tokens: tuple[str, ...] = ()
    forbidden_footer_tokens: tuple[str, ...] = ()
    scope_policy_by_type: tuple[tuple[str, PresencePolicy], ...] = ()
    dependabot_pull_requests: DependabotPullRequestPolicy = DependabotPullRequestPolicy.CHECK
    typos: TyposPolicy = TyposPolicy.SKIP
    quality: QualityPolicy = QualityPolicy()
    body_paragraph_splitting: ParagraphSplittingPolicy = ParagraphSplittingPolicy.SKIP
    required_version: str | None = None
    description_case: DescriptionCasePolicy = DescriptionCasePolicy.ANY
    line_length_urls: LineLengthURLPolicy = LineLengthURLPolicy.CHECK
    footer_max_line_length: int | None = None
    required_colon_footer_tokens: tuple[str, ...] = ()
    length_unit: LengthUnit = LengthUnit.CODEPOINTS
    footer_syntax: FooterSyntax = FooterSyntax.CONVENTIONAL
    skip_pull_request_authors: tuple[str, ...] = ()
    typos_config: TyposConfig = TyposConfig.REPOSITORY
    body_policy_by_type: tuple[tuple[str, PresencePolicy], ...] = ()
    body_max_length: int | None = None
    pull_request_message: PullRequestMessagePolicy = PullRequestMessagePolicy.TITLE_ONLY
    required_issue_prefixes: tuple[str, ...] = ()
    footer_values: tuple[tuple[str, tuple[str, ...]], ...] = ()
    warning_rules: tuple[str, ...] = ()
    message_format: MessageFormat = MessageFormat.CONVENTIONAL


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """One resolved policy and the file that supplied it, if any."""

    policy: CommitPolicy
    path: Path | None


@dataclass(frozen=True, slots=True)
class CommitFooter:
    """One exact source-located footer token start."""

    token: str
    separator: Literal[":", "#"]
    line: int


@dataclass(frozen=True, slots=True)
class ParsedCommit:
    """Parsed message fields shared by explicit conventional and plain formats."""

    message: str
    header: str
    commit_type: str | None
    scope: str | None
    description: str
    breaking: bool
    breaking_header: bool
    breaking_footer: bool
    separator_valid: bool
    body: str
    body_lines: tuple[str, ...]
    body_start_line: int
    footer_lines: tuple[str, ...]
    footer_start_line: int | None


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One stable, machine-addressable policy violation."""

    code: str
    message: str
    line: int = 1
    column: int = 1
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR


@dataclass(frozen=True, slots=True)
class CommitTarget:
    """One message selected from Git or an explicit input source."""

    label: str
    message: str
    sha: str | None = None
    parents: tuple[str, ...] = ()

    @property
    def is_merge(self) -> bool:
        """Return whether Git proved that the commit has multiple parents."""
        return len(self.parents) > 1


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Validation outcome for one target."""

    target: CommitTarget
    header: str
    diagnostics: tuple[Diagnostic, ...] = ()
    skipped_reason: str | None = None

    @property
    def status(self) -> str:
        """Return the stable report status."""
        if self.skipped_reason is not None:
            return "skipped"
        if not self.valid:
            return "failed"
        return "passed"

    @property
    def valid(self) -> bool:
        """Return whether this target allows the invocation to succeed."""
        return not any(d.severity is DiagnosticSeverity.ERROR for d in self.diagnostics)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Complete deterministic validation report."""

    results: tuple[CheckResult, ...]
    config_path: Path | None
    report_version: int = 1

    @property
    def warning_count(self) -> int:
        """Count nonblocking diagnostics, including those on failed targets."""
        return sum(
            d.severity is DiagnosticSeverity.WARNING for r in self.results for d in r.diagnostics
        )

    @property
    def schema_version(self) -> int:
        """Preserve v1 unless warnings are explicitly configured or present."""
        return 2 if self.report_version == 2 or self.warning_count else 1

    @property
    def failed(self) -> int:
        """Return the number of failed commits."""
        return sum(result.status == "failed" for result in self.results)

    @property
    def passed(self) -> int:
        """Return the number of passed commits."""
        return sum(result.status == "passed" for result in self.results)

    @property
    def skipped(self) -> int:
        """Return the number of skipped commits."""
        return sum(result.status == "skipped" for result in self.results)

    @property
    def valid(self) -> bool:
        """Return whether the complete selection passed."""
        return self.failed == 0
