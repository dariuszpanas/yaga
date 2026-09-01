"""Closed rule selection and pure workflow-security evaluation."""

from __future__ import annotations

from collections.abc import Sequence

from yaga.errors import InputError, safe_error_text
from yaga.workflows.models import WorkflowDiagnostic
from yaga.workflows.security_facts import (
    LocatedScalar,
    LocatedValue,
    WorkflowFieldFact,
    WorkflowSecurityFacts,
    WorkflowSecurityStep,
    WorkflowValueKind,
)
from yaga.workflows.security_models import (
    RECOMMENDED_V1_RULES,
    RECOMMENDED_V2_RULES,
    RECOMMENDED_V3_RULES,
    WORKFLOW_SECURITY_RULE_ORDER,
    WorkflowSecurityProfile,
    WorkflowSecurityRule,
    WorkflowSecuritySelection,
)

MAX_SECURITY_RULES = len(WORKFLOW_SECURITY_RULE_ORDER)

_CODE_PREFIX = "security."
_PERSIST_CREDENTIALS_ENV_NAME = "PERSIST-CREDENTIALS"
_PULL_REQUEST_WRITE_PERMISSION_KEYS = frozenset({"pull-requests", "statuses"})
_YAML_BOOLEAN_TAG = "tag:yaml.org,2002:bool"
_YAML_STRING_TAG = "tag:yaml.org,2002:str"
_MESSAGES = {
    WorkflowSecurityRule.PERMISSIONS_EXPLICIT: "workflow must declare top-level permissions",
    WorkflowSecurityRule.PERMISSIONS_TOP_LEVEL_WRITE: (
        "top-level write permission must be scoped to a job"
    ),
    WorkflowSecurityRule.PERMISSIONS_WRITE_ALL: "permissions must not grant write-all",
    WorkflowSecurityRule.SECRETS_INHERIT: (
        "reusable workflow calls must enumerate inherited secrets"
    ),
    WorkflowSecurityRule.CHECKOUT_UNTRUSTED_REF: (
        "privileged workflow must not check out an untrusted event ref"
    ),
    WorkflowSecurityRule.CHECKOUT_PERSIST_CREDENTIALS: (
        "actions/checkout must disable persisted credentials"
    ),
    WorkflowSecurityRule.PERMISSIONS_PULL_REQUEST_WRITE: (
        "pull_request workflows must not grant pull-requests or statuses write access"
    ),
}

_PROFILE_RULES = {
    WorkflowSecurityProfile.RECOMMENDED_V1: RECOMMENDED_V1_RULES,
    WorkflowSecurityProfile.RECOMMENDED_V2: RECOMMENDED_V2_RULES,
    WorkflowSecurityProfile.RECOMMENDED_V3: RECOMMENDED_V3_RULES,
}


def normalize_security_rules(
    *,
    profile: str | WorkflowSecurityProfile | None = None,
    rules: Sequence[str | WorkflowSecurityRule] = (),
) -> WorkflowSecuritySelection:
    """Resolve one named profile or one exact repeatable custom rule set."""
    if profile is not None and rules:
        raise InputError("choose either a workflow security profile or explicit rules")
    if profile is None and not rules:
        return WorkflowSecuritySelection(
            profile=WorkflowSecurityProfile.RECOMMENDED_V1,
            rules=RECOMMENDED_V1_RULES,
        )
    if profile is not None:
        try:
            normalized_profile = WorkflowSecurityProfile(profile)
        except ValueError as error:
            display = safe_error_text(profile, maximum=80)
            raise InputError(f"unknown workflow security profile: {display}") from error
        if normalized_profile is WorkflowSecurityProfile.CUSTOM:
            raise InputError("the custom workflow security profile requires explicit rules")
        return WorkflowSecuritySelection(
            profile=normalized_profile,
            rules=_PROFILE_RULES[normalized_profile],
        )

    if len(rules) > MAX_SECURITY_RULES:
        raise InputError(
            f"workflow security rule selection exceeds the hard {MAX_SECURITY_RULES}-rule limit"
        )
    normalized: list[WorkflowSecurityRule] = []
    for value in rules:
        try:
            rule = WorkflowSecurityRule(value)
        except ValueError as error:
            display = safe_error_text(value, maximum=80)
            raise InputError(f"unknown workflow security rule: {display}") from error
        normalized.append(rule)
    if len(set(normalized)) != len(normalized):
        raise InputError("workflow security rules must not be repeated")
    selected = frozenset(normalized)
    canonical = tuple(rule for rule in WORKFLOW_SECURITY_RULE_ORDER if rule in selected)
    return WorkflowSecuritySelection(
        profile=WorkflowSecurityProfile.CUSTOM,
        rules=canonical,
    )


def evaluate_workflow_security(
    facts: WorkflowSecurityFacts,
    rules: tuple[WorkflowSecurityRule, ...],
) -> tuple[WorkflowDiagnostic, ...]:
    """Evaluate one normalized rule tuple against construction-free facts."""
    selected = frozenset(rules)
    diagnostics: list[WorkflowDiagnostic] = []

    if WorkflowSecurityRule.PERMISSIONS_EXPLICIT in selected and not facts.permissions:
        _diagnose(
            diagnostics,
            WorkflowSecurityRule.PERMISSIONS_EXPLICIT,
            line=facts.root.line,
            column=facts.root.column,
        )

    if WorkflowSecurityRule.PERMISSIONS_TOP_LEVEL_WRITE in selected:
        for permissions in facts.permissions:
            for value in _mapping_values(permissions, entry_value="write"):
                _diagnose_at(
                    diagnostics,
                    WorkflowSecurityRule.PERMISSIONS_TOP_LEVEL_WRITE,
                    value,
                )

    if WorkflowSecurityRule.PERMISSIONS_WRITE_ALL in selected:
        for permissions in facts.permissions:
            if (value := _field_scalar(permissions)) is not None and value.value == "write-all":
                _diagnose_at(
                    diagnostics,
                    WorkflowSecurityRule.PERMISSIONS_WRITE_ALL,
                    value,
                )
        for job in facts.jobs:
            for permissions in job.permissions:
                if (value := _field_scalar(permissions)) is not None and value.value == "write-all":
                    _diagnose_at(
                        diagnostics,
                        WorkflowSecurityRule.PERMISSIONS_WRITE_ALL,
                        value,
                    )

    if WorkflowSecurityRule.SECRETS_INHERIT in selected:
        for job in facts.jobs:
            reusable_call = any(_scalar(value) is not None for value in job.uses)
            if not reusable_call:
                continue
            for value in job.secrets:
                scalar = _scalar(value)
                if scalar is not None and scalar.value == "inherit":
                    _diagnose_at(
                        diagnostics,
                        WorkflowSecurityRule.SECRETS_INHERIT,
                        scalar,
                    )

    if WorkflowSecurityRule.CHECKOUT_UNTRUSTED_REF in selected:
        events = _trigger_events(facts)
        pull_request_target = "pull_request_target" in events
        workflow_run = "workflow_run" in events
        if pull_request_target or workflow_run:
            for job in facts.jobs:
                for step in job.steps:
                    _check_privileged_checkout(
                        diagnostics,
                        step,
                    )

    if WorkflowSecurityRule.CHECKOUT_PERSIST_CREDENTIALS in selected:
        for job in facts.jobs:
            for step in job.steps:
                _check_checkout_persist_credentials(diagnostics, step)

    if (
        WorkflowSecurityRule.PERMISSIONS_PULL_REQUEST_WRITE in selected
        and "pull_request" in _trigger_events(facts)
    ):
        for permissions in facts.permissions:
            for value in _mapping_values_for_keys(
                permissions,
                entry_keys=_PULL_REQUEST_WRITE_PERMISSION_KEYS,
                entry_value="write",
            ):
                _diagnose_at(
                    diagnostics,
                    WorkflowSecurityRule.PERMISSIONS_PULL_REQUEST_WRITE,
                    value,
                )
        for job in facts.jobs:
            for permissions in job.permissions:
                for value in _mapping_values_for_keys(
                    permissions,
                    entry_keys=_PULL_REQUEST_WRITE_PERMISSION_KEYS,
                    entry_value="write",
                ):
                    _diagnose_at(
                        diagnostics,
                        WorkflowSecurityRule.PERMISSIONS_PULL_REQUEST_WRITE,
                        value,
                    )

    diagnostics.sort(key=lambda item: (item.line, item.column, item.code, item.message))
    return tuple(diagnostics)


def _mapping_values(
    field: WorkflowFieldFact,
    *,
    entry_value: str,
) -> tuple[LocatedScalar, ...]:
    if field.value.kind is not WorkflowValueKind.MAPPING:
        return ()
    selected: list[LocatedScalar] = []
    for entry in field.entries:
        entry_key = _scalar(entry.key)
        value = _scalar(entry.value)
        if entry_key is not None and value is not None and value.value == entry_value:
            selected.append(value)
    return tuple(selected)


def _mapping_values_for_keys(
    field: WorkflowFieldFact,
    *,
    entry_keys: frozenset[str],
    entry_value: str,
) -> tuple[LocatedScalar, ...]:
    if field.value.kind is not WorkflowValueKind.MAPPING:
        return ()
    selected: list[LocatedScalar] = []
    for entry in field.entries:
        entry_key = _scalar(entry.key)
        value = _scalar(entry.value)
        if (
            entry_key is not None
            and entry_key.value in entry_keys
            and value is not None
            and value.value == entry_value
        ):
            selected.append(value)
    return tuple(selected)


def _trigger_events(facts: WorkflowSecurityFacts) -> frozenset[str]:
    return frozenset(
        scalar.value
        for trigger in facts.triggers
        for event in trigger.events
        if (scalar := _scalar(event)) is not None
    )


def _field_scalar(field: WorkflowFieldFact) -> LocatedScalar | None:
    return _scalar(field.value)


def _scalar(value: LocatedValue) -> LocatedScalar | None:
    if value.kind is not WorkflowValueKind.SCALAR:
        return None
    return value.scalar


def _check_privileged_checkout(
    diagnostics: list[WorkflowDiagnostic],
    step: WorkflowSecurityStep,
) -> None:
    inputs = tuple(entry for field in step.inputs for entry in field.entries)
    unsafe = any(
        (key := _scalar(entry.key)) is not None
        and key.value.casefold() == "allow-unsafe-pr-checkout"
        and not _literal_false(entry.value)
        for entry in inputs
    )
    if not unsafe:
        unsafe = any(
            (key := _scalar(entry.key)) is not None
            and key.value.casefold() in {"ref", "repository"}
            and (value := _scalar(entry.value)) is not None
            and _dynamic_expression(value.value)
            for entry in inputs
        )
    if not unsafe:
        return

    for uses in step.uses:
        scalar = _scalar(uses)
        if scalar is not None and _is_checkout(scalar.value):
            _diagnose_at(
                diagnostics,
                WorkflowSecurityRule.CHECKOUT_UNTRUSTED_REF,
                scalar,
            )


def _check_checkout_persist_credentials(
    diagnostics: list[WorkflowDiagnostic],
    step: WorkflowSecurityStep,
) -> None:
    checkout_uses = tuple(
        scalar
        for uses in step.uses
        if (scalar := _scalar(uses)) is not None and _is_checkout(scalar.value)
    )
    if not checkout_uses or _persist_credentials_disabled(step):
        return
    for scalar in checkout_uses:
        _diagnose_at(
            diagnostics,
            WorkflowSecurityRule.CHECKOUT_PERSIST_CREDENTIALS,
            scalar,
        )


def _persist_credentials_disabled(step: WorkflowSecurityStep) -> bool:
    if len(step.inputs) != 1:
        return False
    inputs = step.inputs[0]
    if inputs.value.kind is not WorkflowValueKind.MAPPING:
        return False
    candidates: list[tuple[LocatedScalar, LocatedValue]] = []
    for entry in inputs.entries:
        key = _scalar(entry.key)
        if key is None or not key.value.isascii():
            return False
        environment_name = key.value.replace(" ", "_").upper()
        if environment_name == _PERSIST_CREDENTIALS_ENV_NAME:
            candidates.append((key, entry.value))
    if len(candidates) != 1:
        return False
    key, value = candidates[0]
    return key.tag == _YAML_STRING_TAG and _checkout_literal_false(value)


def _checkout_literal_false(value: LocatedValue) -> bool:
    scalar = _scalar(value)
    return (
        scalar is not None
        and scalar.tag in {_YAML_BOOLEAN_TAG, _YAML_STRING_TAG}
        and scalar.value == "false"
    )


def _dynamic_expression(value: str) -> bool:
    return "${{" in value


def _literal_false(value: LocatedValue) -> bool:
    scalar = _scalar(value)
    return scalar is not None and scalar.value == "false"


def _is_checkout(value: str) -> bool:
    path, separator, _revision = value.partition("@")
    if not separator:
        return False
    segments = tuple(segment for segment in path.replace("\\", "/").split("/") if segment)
    return (
        len(segments) >= 2
        and segments[0].isascii()
        and segments[1].isascii()
        and segments[0].lower() == "actions"
        and segments[1].lower() == "checkout"
    )


def _diagnose_at(
    diagnostics: list[WorkflowDiagnostic],
    rule: WorkflowSecurityRule,
    value: LocatedScalar,
) -> None:
    _diagnose(diagnostics, rule, line=value.line, column=value.column)


def _diagnose(
    diagnostics: list[WorkflowDiagnostic],
    rule: WorkflowSecurityRule,
    *,
    line: int,
    column: int,
) -> None:
    diagnostics.append(
        WorkflowDiagnostic(
            code=f"{_CODE_PREFIX}{rule.value}",
            message=_MESSAGES[rule],
            line=line,
            column=column,
        )
    )
