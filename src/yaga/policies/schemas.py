"""Pydantic v2 and pydantic-versions schema families for all YAGA TOML policies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_versions import (
    FieldRemoved,
    MissingSchemaVersionError,
    SchemaFamily,
    SchemaVersion,
    UnknownSchemaVersionError,
    VersionMetadata,
    VersionTransition,
)

from yaga.errors import ConfigurationError
from yaga.version_requirement import check_requirement


class PolicyKind(StrEnum):
    """Canonical policy family identifiers for CLI initialization and auditing."""

    COMMIT = "commit"
    REPO = "repo"
    BRANCH = "branch"
    CHANGE = "change"
    DESCRIPTION = "description"
    MODE = "mode"
    PATH = "path"
    SIZE = "size"
    TREE = "tree"
    AGENT_REVIEW = "agent-review"
    TEST = "test"


class _StrictWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# ---------------------------------------------------------------------------
# 1. Commit configuration (.yaga.toml / [tool.yaga] in pyproject.toml)
# ---------------------------------------------------------------------------


class QualityConfigWire(_StrictWireModel):
    provider: str | None = None
    task: str | None = None
    model: str | None = None
    revision: str | None = None
    threshold: int | float | None = None
    region: str | None = None
    max_tokens: int | None = Field(default=None, alias="max-tokens")
    max_input_tokens: int | None = Field(default=None, alias="max-input-tokens")
    input_mode: str | None = Field(default=None, alias="input-mode")


class CommitSectionWire(_StrictWireModel):
    message_format: str = Field(default="conventional", alias="message-format")
    allowed_types: list[str] | None = Field(default=None, alias="allowed-types")
    type_case: str = Field(default="any", alias="type-case")
    scope_policy: str = Field(default="optional", alias="scope-policy")
    scope_policy_by_type: dict[str, str] = Field(default_factory=dict, alias="scope-policy-by-type")
    allowed_scopes: list[str] | None = Field(default=None, alias="allowed-scopes")
    scope_case: str = Field(default="any", alias="scope-case")
    header_max_length: int | None = Field(default=None, alias="header-max-length")
    description_min_length: int = Field(default=1, alias="description-min-length")
    description_max_length: int | None = Field(default=None, alias="description-max-length")
    description_ending: str = Field(default="allow", alias="description-ending")
    description_case: str = Field(default="any", alias="description-case")
    length_unit: str = Field(default="codepoints", alias="length-unit")
    footer_syntax: str = Field(default="conventional", alias="footer-syntax")
    line_length_urls: str = Field(default="check", alias="line-length-urls")
    footer_max_line_length: int | None = Field(default=None, alias="footer-max-line-length")
    body_policy: str = Field(default="optional", alias="body-policy")
    body_policy_by_type: dict[str, str] = Field(default_factory=dict, alias="body-policy-by-type")
    body_max_line_length: int | None = Field(default=None, alias="body-max-line-length")
    body_min_length: int = Field(default=0, alias="body-min-length")
    body_max_length: int | None = Field(default=None, alias="body-max-length")
    body_min_words: int = Field(default=0, alias="body-min-words")
    body_paragraph_splitting: str = Field(default="skip", alias="body-paragraph-splitting")
    breaking_markers: str = Field(default="either", alias="breaking-markers")
    merge_commits: str = Field(default="ignore", alias="merge-commits")
    ignored_headers: list[str] = Field(default_factory=list, alias="ignored-headers")
    max_commits: int = Field(default=256, alias="max-commits")
    required_footer_tokens: list[str] = Field(default_factory=list, alias="required-footer-tokens")
    required_colon_footer_tokens: list[str] = Field(
        default_factory=list, alias="required-colon-footer-tokens"
    )
    forbidden_footer_tokens: list[str] = Field(
        default_factory=list, alias="forbidden-footer-tokens"
    )
    required_issue_prefixes: list[str] = Field(
        default_factory=list, alias="required-issue-prefixes"
    )
    footer_values: dict[str, list[str]] = Field(default_factory=dict, alias="footer-values")
    warning_rules: list[str] = Field(default_factory=list, alias="warning-rules")
    skip_pull_request_authors: list[str] = Field(
        default_factory=list, alias="skip-pull-request-authors"
    )
    dependabot_pull_requests: str = Field(default="check", alias="dependabot-pull-requests")
    dependabot_require_github_signature: bool = Field(
        default=True, alias="dependabot-require-github-signature"
    )
    typos: str = Field(default="skip")
    typos_config: str = Field(default="repository", alias="typos-config")
    pull_request_message: str = Field(default="title-only", alias="pull-request-message")
    quality: QualityConfigWire | None = None


class CommitConfigWire(_StrictWireModel):
    required_version: str | None = Field(default=None, alias="required-version")
    commit: CommitSectionWire = Field(default_factory=CommitSectionWire)
    agent_review: dict[str, Any] | None = Field(default=None, alias="agent-review")


COMMIT_CONFIG_FAMILY: SchemaFamily[CommitConfigWire] = SchemaFamily(
    model=CommitConfigWire,
    name="commit",
    version_metadata=VersionMetadata(path="config-version", owner="family"),
    missing_version="1",
    versions=(SchemaVersion("1"),),
).compile()


# ---------------------------------------------------------------------------
# 2. Repository check plan (.yaga/checks/ci.toml) — v1 -> v2
# ---------------------------------------------------------------------------


class RepoPlanWire(_StrictWireModel):
    required_version: str | None = Field(default=None, alias="required-version")
    checks: list[str]
    workflow_paths: list[str] | None = Field(default=None, alias="workflow-paths")
    workflow_security_profile: str | None = Field(default=None, alias="workflow-security-profile")
    workflow_security_rules: list[str] | None = Field(default=None, alias="workflow-security-rules")
    mode_policy: str | None = Field(default=None, alias="mode-policy")
    path_policy: str | None = Field(default=None, alias="path-policy")
    size_policy: str | None = Field(default=None, alias="size-policy")
    tree_policy: str | None = Field(default=None, alias="tree-policy")


REPO_PLAN_FAMILY: SchemaFamily[RepoPlanWire] = SchemaFamily(
    model=RepoPlanWire,
    name="repo",
    version_metadata=VersionMetadata(path="plan-version", owner="family"),
    versions=(
        SchemaVersion(
            "1",
            patches=(
                FieldRemoved("required_version"),
                FieldRemoved("mode_policy"),
                FieldRemoved("path_policy"),
                FieldRemoved("size_policy"),
                FieldRemoved("tree_policy"),
            ),
        ),
        SchemaVersion("2"),
    ),
    transitions=(
        VersionTransition(
            source="1",
            target="2",
            upgrade=lambda payload: dict(payload),
        ),
    ),
).compile()


# ---------------------------------------------------------------------------
# 3. Branch policy (.yaga/branch-policy.toml)
# ---------------------------------------------------------------------------


class BranchPolicyWire(_StrictWireModel):
    required_version: str | None = Field(default=None, alias="required-version")
    allowed_patterns: list[str] = Field(alias="allowed-patterns")


BRANCH_POLICY_FAMILY: SchemaFamily[BranchPolicyWire] = SchemaFamily(
    model=BranchPolicyWire,
    name="branch",
    version_metadata=VersionMetadata(path="branch-policy-version", owner="family"),
    versions=(SchemaVersion("1"),),
).compile()


# ---------------------------------------------------------------------------
# 4. Change policy (.yaga/change-policy.toml)
# ---------------------------------------------------------------------------


class ChangeRuleWire(_StrictWireModel):
    name: str
    when_any: list[str] = Field(alias="when-any")
    require_any: list[str] = Field(alias="require-any")


class ChangePolicyWire(_StrictWireModel):
    required_version: str | None = Field(default=None, alias="required-version")
    rules: list[ChangeRuleWire]


CHANGE_POLICY_FAMILY: SchemaFamily[ChangePolicyWire] = SchemaFamily(
    model=ChangePolicyWire,
    name="change",
    version_metadata=VersionMetadata(path="change-policy-version", owner="family"),
    versions=(SchemaVersion("1"),),
).compile()


# ---------------------------------------------------------------------------
# 5. Description policy (.yaga/description-policy.toml)
# ---------------------------------------------------------------------------


class DescriptionPolicyWire(_StrictWireModel):
    required_version: str | None = Field(default=None, alias="required-version")
    min_words: int = Field(default=1, alias="min-words")
    max_length: int = Field(default=65536, alias="max-length")
    placeholders: list[str] = Field(default_factory=lambda: ["TODO", "TBD"])
    require_checked_tasks: bool = Field(default=False, alias="require-checked-tasks")
    typos: str = Field(default="skip")
    heading_min_level: int = Field(default=2, alias="heading-min-level")
    heading_max_length: int = Field(default=120, alias="heading-max-length")


DESCRIPTION_POLICY_FAMILY: SchemaFamily[DescriptionPolicyWire] = SchemaFamily(
    model=DescriptionPolicyWire,
    name="description",
    version_metadata=VersionMetadata(path="description-policy-version", owner="family"),
    versions=(SchemaVersion("1"),),
).compile()


# ---------------------------------------------------------------------------
# 6. Mode policy (.yaga/mode-policy.toml)
# ---------------------------------------------------------------------------


class ModePathOverrideWire(_StrictWireModel):
    pattern: str
    allowed_modes: list[str] = Field(alias="allowed-modes")


class ModePolicyWire(_StrictWireModel):
    required_version: str | None = Field(default=None, alias="required-version")
    default_allowed_modes: list[str] = Field(alias="default-allowed-modes")
    path_overrides: list[ModePathOverrideWire] = Field(default_factory=list, alias="path-overrides")


MODE_POLICY_FAMILY: SchemaFamily[ModePolicyWire] = SchemaFamily(
    model=ModePolicyWire,
    name="mode",
    version_metadata=VersionMetadata(path="mode-policy-version", owner="family"),
    versions=(SchemaVersion("1"),),
).compile()


# ---------------------------------------------------------------------------
# 7. Path policy (.yaga/path-policy.toml)
# ---------------------------------------------------------------------------


class PathPolicyWire(_StrictWireModel):
    required_version: str | None = Field(default=None, alias="required-version")
    profile: str | None = None
    rules: list[str] | None = None


PATH_POLICY_FAMILY: SchemaFamily[PathPolicyWire] = SchemaFamily(
    model=PathPolicyWire,
    name="path",
    version_metadata=VersionMetadata(path="path-policy-version", owner="family"),
    versions=(SchemaVersion("1"),),
).compile()


# ---------------------------------------------------------------------------
# 8. Size policy (.yaga/size-policy.toml)
# ---------------------------------------------------------------------------


class SizePathLimitWire(_StrictWireModel):
    pattern: str
    max_blob_bytes: int = Field(alias="max-blob-bytes")


class SizePolicyWire(_StrictWireModel):
    required_version: str | None = Field(default=None, alias="required-version")
    default_max_blob_bytes: int = Field(alias="default-max-blob-bytes")
    max_total_blob_bytes: int | None = Field(default=None, alias="max-total-blob-bytes")
    path_limits: list[SizePathLimitWire] = Field(default_factory=list, alias="path-limits")


SIZE_POLICY_FAMILY: SchemaFamily[SizePolicyWire] = SchemaFamily(
    model=SizePolicyWire,
    name="size",
    version_metadata=VersionMetadata(path="size-policy-version", owner="family"),
    versions=(SchemaVersion("1"),),
).compile()


# ---------------------------------------------------------------------------
# 9. Tree policy (.yaga/tree-policy.toml)
# ---------------------------------------------------------------------------


class TreePolicyWire(_StrictWireModel):
    required_version: str | None = Field(default=None, alias="required-version")
    required_paths: list[str] = Field(alias="required-paths")
    forbidden_patterns: list[str] = Field(alias="forbidden-patterns")


TREE_POLICY_FAMILY: SchemaFamily[TreePolicyWire] = SchemaFamily(
    model=TreePolicyWire,
    name="tree",
    version_metadata=VersionMetadata(path="tree-policy-version", owner="family"),
    versions=(SchemaVersion("1"),),
).compile()


# ---------------------------------------------------------------------------
# 10. Agent review policy (.yaga/agent-review-policy.toml / [agent-review])
# ---------------------------------------------------------------------------


class AgentReviewLensWire(_StrictWireModel):
    preset: str | None = None
    outcome: str = "review"
    publication: str = "review"
    bot_login: str | None = Field(default=None, alias="bot-login")
    bot_user_id: int | None = Field(default=None, alias="bot-user-id")
    bot_app_id: int | None = Field(default=None, alias="bot-app-id")
    pass_reactions: list[str] | None = Field(default=None, alias="pass-reactions")
    pending_reactions: list[str] | None = Field(default=None, alias="pending-reactions")
    check_runs: list[str] | None = Field(default=None, alias="check-runs")
    delay_seconds: int = Field(default=0, alias="delay-seconds")
    wait_seconds: int = Field(default=0, alias="wait-seconds")


class AgentReviewPolicyWire(_StrictWireModel):
    required_version: str | None = Field(default=None, alias="required-version")
    required: list[str]
    aggregation: str = "all-required"
    agents: dict[str, AgentReviewLensWire] | None = None


AGENT_REVIEW_POLICY_FAMILY: SchemaFamily[AgentReviewPolicyWire] = SchemaFamily(
    model=AgentReviewPolicyWire,
    name="agent-review",
    version_metadata=VersionMetadata(path="version", owner="family"),
    versions=(SchemaVersion("1"),),
).compile()


# ---------------------------------------------------------------------------
# 11. Test qualification policy (.yaga/test-policy.toml)
# ---------------------------------------------------------------------------


class TestSuiteWire(_StrictWireModel):
    id: str
    binding: str
    tags: list[str] = Field(default_factory=list)
    variants: list[str] = Field(default_factory=lambda: ["default"])
    needs: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)


class TestProfileWire(_StrictWireModel):
    mode: str
    always: list[str] = Field(default_factory=list)


class TestRuleWire(_StrictWireModel):
    id: str
    when_any: list[str] = Field(alias="when-any")
    select: list[str] = Field(default_factory=list)
    select_tags: list[str] = Field(default_factory=list, alias="select-tags")


class TestPolicyWire(_StrictWireModel):
    required_version: str | None = Field(default=None, alias="required-version")
    suites: list[TestSuiteWire]
    profiles: dict[str, TestProfileWire]
    unknown_paths: str = Field(default="full", alias="unknown-paths")
    full_on: list[str] = Field(default_factory=list, alias="full-on")
    no_impact: list[str] = Field(default_factory=list, alias="no-impact")
    rules: list[TestRuleWire] = Field(default_factory=list)


TEST_POLICY_FAMILY: SchemaFamily[TestPolicyWire] = SchemaFamily(
    model=TestPolicyWire,
    name="test",
    version_metadata=VersionMetadata(path="test-policy-version", owner="family"),
    versions=(SchemaVersion("1"),),
).compile()


# ---------------------------------------------------------------------------
# Registry & integer-discriminator validation bridge
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicySpec:
    """Metadata and compiled SchemaFamily for one YAGA TOML policy type."""

    kind: PolicyKind
    title: str
    default_relpath: str
    version_key: str
    family: SchemaFamily[Any]
    description: str

    @property
    def current_version_int(self) -> int:
        return int(self.family.current_version)

    @property
    def supported_versions_int(self) -> tuple[int, ...]:
        return tuple(int(item.label) for item in self.family.versions)


POLICY_SPECS: dict[PolicyKind, PolicySpec] = {
    PolicyKind.COMMIT: PolicySpec(
        kind=PolicyKind.COMMIT,
        title="Commit policy",
        default_relpath=".yaga.toml",
        version_key="config-version",
        family=COMMIT_CONFIG_FAMILY,
        description="Conventional Commit types, scopes, body rules, and PR title checks",
    ),
    PolicyKind.REPO: PolicySpec(
        kind=PolicyKind.REPO,
        title="Repository check plan",
        default_relpath=".yaga/checks/ci.toml",
        version_key="plan-version",
        family=REPO_PLAN_FAMILY,
        description="Explicit repository check providers and committed-tree policy bindings",
    ),
    PolicyKind.BRANCH: PolicySpec(
        kind=PolicyKind.BRANCH,
        title="Branch name policy",
        default_relpath=".yaga/branch-policy.toml",
        version_key="branch-policy-version",
        family=BRANCH_POLICY_FAMILY,
        description="Allowed branch-name patterns (feat/*, fix/*, docs/*, ...)",
    ),
    PolicyKind.CHANGE: PolicySpec(
        kind=PolicyKind.CHANGE,
        title="Change coupling policy",
        default_relpath=".yaga/change-policy.toml",
        version_key="change-policy-version",
        family=CHANGE_POLICY_FAMILY,
        description="Co-change rules (e.g. source edits require tests or docs)",
    ),
    PolicyKind.DESCRIPTION: PolicySpec(
        kind=PolicyKind.DESCRIPTION,
        title="PR description policy",
        default_relpath=".yaga/description-policy.toml",
        version_key="description-policy-version",
        family=DESCRIPTION_POLICY_FAMILY,
        description="Pull-request body word floor, heading structure, and placeholder checks",
    ),
    PolicyKind.MODE: PolicySpec(
        kind=PolicyKind.MODE,
        title="Git file-mode policy",
        default_relpath=".yaga/mode-policy.toml",
        version_key="mode-policy-version",
        family=MODE_POLICY_FAMILY,
        description="Allowed committed Git modes (regular 100644, executable scripts, ...)",
    ),
    PolicyKind.PATH: PolicySpec(
        kind=PolicyKind.PATH,
        title="Portable path policy",
        default_relpath=".yaga/path-policy.toml",
        version_key="path-policy-version",
        family=PATH_POLICY_FAMILY,
        description="Cross-platform path portability and case-collision protection",
    ),
    PolicyKind.SIZE: PolicySpec(
        kind=PolicyKind.SIZE,
        title="Blob size policy",
        default_relpath=".yaga/size-policy.toml",
        version_key="size-policy-version",
        family=SIZE_POLICY_FAMILY,
        description="Maximum tracked blob bytes and path-specific size overrides",
    ),
    PolicyKind.TREE: PolicySpec(
        kind=PolicyKind.TREE,
        title="Tracked tree policy",
        default_relpath=".yaga/tree-policy.toml",
        version_key="tree-policy-version",
        family=TREE_POLICY_FAMILY,
        description="Required repository files and forbidden committed path patterns",
    ),
    PolicyKind.AGENT_REVIEW: PolicySpec(
        kind=PolicyKind.AGENT_REVIEW,
        title="Agent review policy",
        default_relpath=".yaga/agent-review-policy.toml",
        version_key="version",
        family=AGENT_REVIEW_POLICY_FAMILY,
        description="Pull-request AI review lenses (codex, copilot, claude, custom)",
    ),
    PolicyKind.TEST: PolicySpec(
        kind=PolicyKind.TEST,
        title="Test qualification policy",
        default_relpath=".yaga/test-policy.toml",
        version_key="test-policy-version",
        family=TEST_POLICY_FAMILY,
        description="Deterministic changed-path test suite and profile selection",
    ),
}


@dataclass(frozen=True, slots=True)
class VersionedTomlOutcome[ModelT: BaseModel]:
    """Validated pydantic-versions outcome with integer version numbers."""

    source_version: int
    current_version: int
    source_model: BaseModel
    current_model: ModelT
    migrations_applied: bool
    required_version: str | None

    @property
    def is_deprecated(self) -> bool:
        return self.source_version < self.current_version


def check_optional_required_version(
    document: Mapping[str, object],
    *,
    allow_required_version: bool = True,
) -> str | None:
    """Validate optional required-version before rejecting newer schema numbers."""
    if not allow_required_version or "required-version" not in document:
        return None
    return check_requirement(document["required-version"])


def validate_family_wire[ModelT: BaseModel](
    family: SchemaFamily[ModelT],
    document: Mapping[str, object],
    *,
    version_key: str,
    version_error_message: str,
    missing_version_message: str | None = None,
    allow_required_version: bool = True,
) -> VersionedTomlOutcome[ModelT]:
    """Validate a TOML mapping against a compiled pydantic-versions SchemaFamily."""
    required_version = check_optional_required_version(
        document,
        allow_required_version=allow_required_version,
    )
    if version_key not in document:
        if family.missing_version is None:
            raise ConfigurationError(missing_version_message or f"missing {version_key}")
        wire_payload = dict(document)
    else:
        raw_version = document[version_key]
        if type(raw_version) is not int:
            raise ConfigurationError(version_error_message)
        wire_payload = dict(document)
        wire_payload[version_key] = str(raw_version)

    try:
        validated = family.validate(wire_payload)
    except MissingSchemaVersionError as error:
        raise ConfigurationError(missing_version_message or f"missing {version_key}") from error
    except UnknownSchemaVersionError as error:
        raw_int = document.get(version_key)
        current_int = int(family.current_version)
        if type(raw_int) is int and raw_int > current_int:
            raise ConfigurationError(
                f"{version_error_message} (found newer schema version {raw_int}; "
                "update YAGA using its installation manager)"
            ) from error
        raise ConfigurationError(version_error_message) from error
    except ValidationError as error:
        first = error.errors()[0]
        loc = ".".join(str(part) for part in first.get("loc", ()))
        msg = first.get("msg", "validation failed")
        raise ConfigurationError(f"{loc}: {msg}" if loc else msg) from error

    return VersionedTomlOutcome(
        source_version=int(validated.source_version),
        current_version=int(validated.current_version),
        source_model=validated.source_model,
        current_model=validated.current_model,
        migrations_applied=bool(validated.migrations_applied),
        required_version=required_version,
    )
