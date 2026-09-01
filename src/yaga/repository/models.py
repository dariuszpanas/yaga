"""Immutable models for explicit aggregate repository checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from yaga.commits.models import ValidationReport
from yaga.errors import YagaError
from yaga.modes.service import CheckedMode
from yaga.paths.service import CheckedPath
from yaga.sizes.service import CheckedSize
from yaga.trees.service import CheckedTree
from yaga.workflows.models import WorkflowLintReport, WorkflowReport
from yaga.workflows.security_models import (
    WORKFLOW_SECURITY_RULE_ORDER,
    WorkflowSecurityProfile,
    WorkflowSecurityReport,
    WorkflowSecurityRule,
)

type ProviderReport = (
    ValidationReport
    | WorkflowReport
    | WorkflowSecurityReport
    | WorkflowLintReport
    | CheckedMode
    | CheckedPath
    | CheckedSize
    | CheckedTree
)


class RepositoryProvider(StrEnum):
    """Closed set of providers available to one aggregate invocation."""

    COMMIT = "commit"
    WORKFLOW = "workflow"
    WORKFLOW_SECURITY = "workflow-security"
    WORKFLOW_LINT = "workflow-lint"
    MODE = "mode"
    PATH = "path"
    SIZE = "size"
    TREE = "tree"


REPOSITORY_PLAN_V1 = 1
REPOSITORY_PLAN_V2 = 2
REPOSITORY_PLAN_VERSIONS = frozenset({REPOSITORY_PLAN_V1, REPOSITORY_PLAN_V2})

REPOSITORY_PROVIDER_ORDER = (
    RepositoryProvider.COMMIT,
    RepositoryProvider.WORKFLOW,
    RepositoryProvider.WORKFLOW_SECURITY,
    RepositoryProvider.WORKFLOW_LINT,
    RepositoryProvider.MODE,
    RepositoryProvider.PATH,
    RepositoryProvider.SIZE,
    RepositoryProvider.TREE,
)
REPOSITORY_PLAN_V1_PROVIDER_ORDER = (
    RepositoryProvider.COMMIT,
    RepositoryProvider.WORKFLOW,
    RepositoryProvider.WORKFLOW_SECURITY,
    RepositoryProvider.WORKFLOW_LINT,
)
REPOSITORY_PLAN_V2_PROVIDER_ORDER = (
    RepositoryProvider.COMMIT,
    RepositoryProvider.WORKFLOW,
    RepositoryProvider.WORKFLOW_SECURITY,
    RepositoryProvider.WORKFLOW_LINT,
    RepositoryProvider.MODE,
    RepositoryProvider.PATH,
    RepositoryProvider.SIZE,
    RepositoryProvider.TREE,
)
REPOSITORY_PLAN_V1_PROVIDERS = frozenset(REPOSITORY_PLAN_V1_PROVIDER_ORDER)
REPOSITORY_PLAN_V2_PROVIDERS = frozenset(REPOSITORY_PLAN_V2_PROVIDER_ORDER)

WORKFLOW_REPOSITORY_PROVIDERS = frozenset(
    {
        RepositoryProvider.WORKFLOW,
        RepositoryProvider.WORKFLOW_SECURITY,
        RepositoryProvider.WORKFLOW_LINT,
    }
)

POLICY_REPOSITORY_PROVIDERS = frozenset(
    {
        RepositoryProvider.MODE,
        RepositoryProvider.PATH,
        RepositoryProvider.SIZE,
        RepositoryProvider.TREE,
    }
)


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
class RepositoryCheckPlan:
    """One immutable, normalized repository-check plan."""

    plan_version: int
    checks: tuple[RepositoryProvider, ...]
    workflow_paths: tuple[PurePosixPath, ...] = ()
    workflow_security_profile: WorkflowSecurityProfile | None = None
    workflow_security_rules: tuple[WorkflowSecurityRule, ...] = ()
    mode_policy_path: PurePosixPath | None = None
    path_policy_path: PurePosixPath | None = None
    size_policy_path: PurePosixPath | None = None
    tree_policy_path: PurePosixPath | None = None

    def __post_init__(self) -> None:
        if type(self.plan_version) is not int or self.plan_version not in REPOSITORY_PLAN_VERSIONS:
            raise ValueError("repository check plan version must be 1 or 2")
        if not self.checks or any(not isinstance(item, RepositoryProvider) for item in self.checks):
            raise ValueError("repository check plan requires known providers")
        selected = frozenset(self.checks)
        provider_order = (
            REPOSITORY_PLAN_V1_PROVIDER_ORDER
            if self.plan_version == REPOSITORY_PLAN_V1
            else REPOSITORY_PLAN_V2_PROVIDER_ORDER
        )
        allowed_providers = frozenset(provider_order)
        canonical_checks = tuple(provider for provider in provider_order if provider in selected)
        if len(selected) != len(self.checks) or self.checks != canonical_checks:
            raise ValueError("repository check plan providers must be unique and canonical")
        if not selected <= allowed_providers:
            raise ValueError("repository check plan version does not support selected providers")
        if any(type(path) is not PurePosixPath for path in self.workflow_paths):
            raise ValueError("repository check plan workflow paths must be portable paths")
        normalized_paths = {path.as_posix().casefold() for path in self.workflow_paths}
        if len(normalized_paths) != len(self.workflow_paths):
            raise ValueError("repository check plan workflow paths must be unique")
        if self.workflow_security_profile is not None and not isinstance(
            self.workflow_security_profile, WorkflowSecurityProfile
        ):
            raise ValueError("repository check plan profile must be a known profile")
        if self.workflow_security_profile is WorkflowSecurityProfile.CUSTOM:
            raise ValueError("repository check plan cannot select the custom profile by name")
        if self.workflow_security_profile is not None and self.workflow_security_rules:
            raise ValueError("repository check plan cannot combine a profile and explicit rules")
        if any(not isinstance(rule, WorkflowSecurityRule) for rule in self.workflow_security_rules):
            raise ValueError("repository check plan rules must be known rules")
        selected_rules = frozenset(self.workflow_security_rules)
        canonical_rules = tuple(
            rule for rule in WORKFLOW_SECURITY_RULE_ORDER if rule in selected_rules
        )
        if (
            len(selected_rules) != len(self.workflow_security_rules)
            or self.workflow_security_rules != canonical_rules
        ):
            raise ValueError("repository check plan rules must be unique and canonical")
        if self.workflow_paths and selected.isdisjoint(WORKFLOW_REPOSITORY_PROVIDERS):
            raise ValueError("repository check plan workflow paths require a workflow provider")
        if (
            self.workflow_security_profile is not None or self.workflow_security_rules
        ) and RepositoryProvider.WORKFLOW_SECURITY not in selected:
            raise ValueError("repository check plan security policy requires its provider")

        policy_paths = (
            (RepositoryProvider.MODE, self.mode_policy_path),
            (RepositoryProvider.PATH, self.path_policy_path),
            (RepositoryProvider.SIZE, self.size_policy_path),
            (RepositoryProvider.TREE, self.tree_policy_path),
        )
        if any(path is not None and type(path) is not PurePosixPath for _, path in policy_paths):
            raise ValueError("repository check plan policy paths must be portable paths")
        provided_policy_paths = tuple(path for _, path in policy_paths if path is not None)
        if len({path.as_posix().casefold() for path in provided_policy_paths}) != len(
            provided_policy_paths
        ):
            raise ValueError("repository check plan policy paths must be unique")
        for provider, policy_path in policy_paths:
            if (provider in selected) != (policy_path is not None):
                raise ValueError(
                    "repository check plan policy paths are required exactly for selected providers"
                )


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
        if isinstance(self.report, CheckedMode | CheckedPath | CheckedSize | CheckedTree):
            valid = self.report.report.valid
        else:
            valid = self.report.valid
        if valid:
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
