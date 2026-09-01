"""Immutable models for GitHub workflow checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReferenceContext(StrEnum):
    """The GitHub workflow location that owns one ``uses`` reference."""

    STEP = "step"
    JOB = "job"


class WorkflowOutputFormat(StrEnum):
    """Supported workflow-policy report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


@dataclass(frozen=True, slots=True)
class WorkflowDiagnostic:
    """One stable workflow-policy diagnostic."""

    code: str
    message: str
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class WorkflowReference:
    """One bounded ``uses`` scalar selected from a workflow."""

    value: str
    context: ReferenceContext
    line: int
    column: int
    source_start: int | None = None
    source_end: int | None = None


@dataclass(frozen=True, slots=True)
class ActionManifestDependency:
    """One action-runtime file reference selected without constructing YAML."""

    field: str
    value: str


@dataclass(frozen=True, slots=True)
class ParsedActionManifest:
    """Bounded action-manifest data needed to reproduce actionlint file checks."""

    dependencies: tuple[ActionManifestDependency, ...]
    node_count: int


@dataclass(frozen=True, slots=True)
class ParsedWorkflow:
    """Bounded representation selected from one YAML workflow."""

    references: tuple[WorkflowReference, ...]
    diagnostics: tuple[WorkflowDiagnostic, ...]
    node_count: int


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    """Complete policy outcome for one repository-relative workflow."""

    path: str
    references_checked: int
    diagnostics: tuple[WorkflowDiagnostic, ...]

    @property
    def valid(self) -> bool:
        """Return whether the workflow has no policy diagnostics."""
        return not self.diagnostics

    @property
    def status(self) -> str:
        """Return the stable status label."""
        return "passed" if self.valid else "failed"


@dataclass(frozen=True, slots=True)
class WorkflowReport:
    """Deterministic aggregate workflow-policy report."""

    results: tuple[WorkflowResult, ...]

    @property
    def checked(self) -> int:
        """Return the number of checked workflow files."""
        return len(self.results)

    @property
    def passed(self) -> int:
        """Return the number of passing workflow files."""
        return sum(result.valid for result in self.results)

    @property
    def failed(self) -> int:
        """Return the number of failing workflow files."""
        return self.checked - self.passed

    @property
    def references_checked(self) -> int:
        """Return the number of selected ``uses`` references."""
        return sum(result.references_checked for result in self.results)

    @property
    def valid(self) -> bool:
        """Return whether every workflow passed."""
        return self.failed == 0


@dataclass(frozen=True, slots=True)
class WorkflowLintResult:
    """Complete actionlint outcome for one repository-relative workflow."""

    path: str
    diagnostics: tuple[WorkflowDiagnostic, ...]

    @property
    def valid(self) -> bool:
        """Return whether actionlint reported no diagnostics."""
        return not self.diagnostics

    @property
    def status(self) -> str:
        """Return the stable status label."""
        return "passed" if self.valid else "failed"


@dataclass(frozen=True, slots=True)
class WorkflowLintReport:
    """Deterministic aggregate actionlint report."""

    results: tuple[WorkflowLintResult, ...]

    @property
    def checked(self) -> int:
        """Return the number of linted workflow files."""
        return len(self.results)

    @property
    def passed(self) -> int:
        """Return the number of workflows without lint diagnostics."""
        return sum(result.valid for result in self.results)

    @property
    def failed(self) -> int:
        """Return the number of workflows with lint diagnostics."""
        return self.checked - self.passed

    @property
    def diagnostics(self) -> int:
        """Return the total number of actionlint diagnostics."""
        return sum(len(result.diagnostics) for result in self.results)

    @property
    def valid(self) -> bool:
        """Return whether every workflow passed actionlint."""
        return self.failed == 0
