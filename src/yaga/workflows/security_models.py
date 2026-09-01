"""Immutable models for pure-Python GitHub workflow security policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from yaga.workflows.models import WorkflowDiagnostic


class WorkflowSecurityProfile(StrEnum):
    """Closed workflow-security rule profiles."""

    RECOMMENDED_V1 = "recommended-v1"
    RECOMMENDED_V2 = "recommended-v2"
    RECOMMENDED_V3 = "recommended-v3"
    CUSTOM = "custom"


class WorkflowSecurityRule(StrEnum):
    """Stable identifiers for independently selectable security rules."""

    PERMISSIONS_EXPLICIT = "permissions.explicit"
    PERMISSIONS_TOP_LEVEL_WRITE = "permissions.top_level_write"
    PERMISSIONS_WRITE_ALL = "permissions.write_all"
    SECRETS_INHERIT = "secrets.inherit"
    CHECKOUT_UNTRUSTED_REF = "checkout.untrusted_ref"
    CHECKOUT_PERSIST_CREDENTIALS = "checkout.persist_credentials"
    PERMISSIONS_PULL_REQUEST_WRITE = "permissions.pull_request_write"


WORKFLOW_SECURITY_RULE_ORDER = tuple(WorkflowSecurityRule)
RECOMMENDED_V1_RULES = (
    WorkflowSecurityRule.PERMISSIONS_EXPLICIT,
    WorkflowSecurityRule.PERMISSIONS_TOP_LEVEL_WRITE,
    WorkflowSecurityRule.PERMISSIONS_WRITE_ALL,
    WorkflowSecurityRule.SECRETS_INHERIT,
    WorkflowSecurityRule.CHECKOUT_UNTRUSTED_REF,
)
RECOMMENDED_V2_RULES = (
    *RECOMMENDED_V1_RULES,
    WorkflowSecurityRule.CHECKOUT_PERSIST_CREDENTIALS,
)
RECOMMENDED_V3_RULES = (
    *RECOMMENDED_V2_RULES,
    WorkflowSecurityRule.PERMISSIONS_PULL_REQUEST_WRITE,
)


@dataclass(frozen=True, slots=True)
class WorkflowSecuritySelection:
    """One normalized profile or exact custom rule selection."""

    profile: WorkflowSecurityProfile
    rules: tuple[WorkflowSecurityRule, ...]

    def __post_init__(self) -> None:
        _require_canonical_rules(self.profile, self.rules)


@dataclass(frozen=True, slots=True)
class WorkflowSecurityResult:
    """Complete security-policy outcome for one workflow."""

    path: str
    diagnostics: tuple[WorkflowDiagnostic, ...]

    @property
    def valid(self) -> bool:
        """Return whether the workflow has no security diagnostics."""
        return not self.diagnostics

    @property
    def status(self) -> str:
        """Return the stable status label."""
        return "passed" if self.valid else "failed"


@dataclass(frozen=True, slots=True)
class WorkflowSecurityReport:
    """Deterministic aggregate security-policy report."""

    profile: WorkflowSecurityProfile
    rules: tuple[WorkflowSecurityRule, ...]
    results: tuple[WorkflowSecurityResult, ...]

    def __post_init__(self) -> None:
        _require_canonical_rules(self.profile, self.rules)

    @property
    def checked(self) -> int:
        """Return the number of checked workflow files."""
        return len(self.results)

    @property
    def passed(self) -> int:
        """Return the number of workflows without security diagnostics."""
        return sum(result.valid for result in self.results)

    @property
    def failed(self) -> int:
        """Return the number of workflows with security diagnostics."""
        return self.checked - self.passed

    @property
    def diagnostics(self) -> int:
        """Return the total number of security diagnostics."""
        return sum(len(result.diagnostics) for result in self.results)

    @property
    def valid(self) -> bool:
        """Return whether every workflow passed security policy."""
        return self.failed == 0


def _require_canonical_rules(
    profile: WorkflowSecurityProfile,
    rules: tuple[WorkflowSecurityRule, ...],
) -> None:
    if not rules:
        raise ValueError("workflow security rules must be nonempty")
    selected = frozenset(rules)
    canonical = tuple(rule for rule in WORKFLOW_SECURITY_RULE_ORDER if rule in selected)
    if len(selected) != len(rules) or canonical != rules:
        raise ValueError("workflow security rules must be unique and canonical")
    if profile is WorkflowSecurityProfile.RECOMMENDED_V1 and rules != RECOMMENDED_V1_RULES:
        raise ValueError("recommended-v1 requires its complete canonical rule set")
    if profile is WorkflowSecurityProfile.RECOMMENDED_V2 and rules != RECOMMENDED_V2_RULES:
        raise ValueError("recommended-v2 requires its complete canonical rule set")
    if profile is WorkflowSecurityProfile.RECOMMENDED_V3 and rules != RECOMMENDED_V3_RULES:
        raise ValueError("recommended-v3 requires its complete canonical rule set")
