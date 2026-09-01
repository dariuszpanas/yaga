"""Immutable models for explicit aggregate repository checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from yaga.commits.models import ValidationReport
from yaga.errors import YagaError
from yaga.workflows.models import WorkflowLintReport, WorkflowReport
from yaga.workflows.security_models import WorkflowSecurityReport

type ProviderReport = (
    ValidationReport | WorkflowReport | WorkflowSecurityReport | WorkflowLintReport
)


class RepositoryProvider(StrEnum):
    """Closed set of providers available to one aggregate invocation."""

    COMMIT = "commit"
    WORKFLOW = "workflow"
    WORKFLOW_SECURITY = "workflow-security"
    WORKFLOW_LINT = "workflow-lint"


class RepositoryOutputFormat(StrEnum):
    """Stable aggregate report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


class RepositoryCheckStatus(StrEnum):
    """Outcome of one selected provider."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RepositoryCheckResult:
    """One provider report or one expected operational error."""

    provider: RepositoryProvider
    report: ProviderReport | None = None
    error: YagaError | None = None

    def __post_init__(self) -> None:
        if (self.report is None) == (self.error is None):
            raise ValueError("repository check result requires exactly one report or error")

    @property
    def status(self) -> RepositoryCheckStatus:
        """Return the stable provider outcome."""
        if self.error is not None:
            return RepositoryCheckStatus.ERROR
        assert self.report is not None
        if self.report.valid:
            return RepositoryCheckStatus.PASSED
        return RepositoryCheckStatus.FAILED


@dataclass(frozen=True, slots=True)
class RepositoryReport:
    """Deterministic aggregate of explicitly selected providers."""

    checks: tuple[RepositoryCheckResult, ...]

    @property
    def selected(self) -> int:
        """Return the number of selected providers."""
        return len(self.checks)

    @property
    def passed(self) -> int:
        """Return the number of providers that passed."""
        return sum(check.status is RepositoryCheckStatus.PASSED for check in self.checks)

    @property
    def failed(self) -> int:
        """Return the number of providers with policy findings."""
        return sum(check.status is RepositoryCheckStatus.FAILED for check in self.checks)

    @property
    def errored(self) -> int:
        """Return the number of providers with operational errors."""
        return sum(check.status is RepositoryCheckStatus.ERROR for check in self.checks)

    @property
    def status(self) -> RepositoryCheckStatus:
        """Return the aggregate status with operational errors taking precedence."""
        if self.errored:
            return RepositoryCheckStatus.ERROR
        if self.failed:
            return RepositoryCheckStatus.FAILED
        return RepositoryCheckStatus.PASSED

    @property
    def valid(self) -> bool:
        """Return whether every selected provider passed."""
        return self.status is RepositoryCheckStatus.PASSED
