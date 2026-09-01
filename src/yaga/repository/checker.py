"""Explicit orchestration for aggregate local repository checks."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yaga.commits.service import check_git_commits
from yaga.errors import InputError, YagaError, safe_error_text
from yaga.repository.models import (
    RepositoryCheckResult,
    RepositoryProvider,
    RepositoryReport,
)
from yaga.workflows.checker import check_parsed_workflow_inputs
from yaga.workflows.inputs import WorkflowInput, load_workflow_inputs
from yaga.workflows.lint import lint_workflow_inputs
from yaga.workflows.parser import ParsedWorkflowInput, parse_workflow_inputs
from yaga.workflows.security import check_parsed_workflow_security_inputs
from yaga.workflows.security_models import WorkflowSecurityProfile, WorkflowSecurityRule
from yaga.workflows.security_rules import normalize_security_rules

_PROVIDER_ORDER = (
    RepositoryProvider.COMMIT,
    RepositoryProvider.WORKFLOW,
    RepositoryProvider.WORKFLOW_SECURITY,
    RepositoryProvider.WORKFLOW_LINT,
)
_WORKFLOW_PROVIDERS = frozenset(
    {
        RepositoryProvider.WORKFLOW,
        RepositoryProvider.WORKFLOW_SECURITY,
        RepositoryProvider.WORKFLOW_LINT,
    }
)
_PARSED_WORKFLOW_PROVIDERS = frozenset(
    {RepositoryProvider.WORKFLOW, RepositoryProvider.WORKFLOW_SECURITY}
)


def check_repository(
    repository: Path,
    providers: Sequence[str | RepositoryProvider],
    *,
    config: Path | None = None,
    commit: str | None = None,
    revision_range: str | None = None,
    workflow_paths: Sequence[Path] = (),
    workflow_security_profile: str | WorkflowSecurityProfile | None = None,
    workflow_security_rules: Sequence[str | WorkflowSecurityRule] = (),
) -> RepositoryReport:
    """Run a closed, explicit provider set in canonical order."""
    selected = _normalize_providers(providers)
    _validate_provider_arguments(
        selected,
        config=config,
        commit=commit,
        revision_range=revision_range,
        workflow_paths=workflow_paths,
        workflow_security_profile=workflow_security_profile,
        workflow_security_rules=workflow_security_rules,
    )
    if RepositoryProvider.WORKFLOW_SECURITY in selected:
        normalize_security_rules(
            profile=workflow_security_profile,
            rules=workflow_security_rules,
        )

    workflow_inputs_loaded = False
    workflow_inputs: tuple[WorkflowInput, ...] | None = None
    workflow_input_error: YagaError | None = None
    workflow_inputs_parsed = False
    parsed_workflow_inputs: tuple[ParsedWorkflowInput, ...] | None = None
    workflow_parse_error: YagaError | None = None

    checks: list[RepositoryCheckResult] = []
    for provider in _PROVIDER_ORDER:
        if provider not in selected:
            continue
        if provider in _WORKFLOW_PROVIDERS and not workflow_inputs_loaded:
            workflow_inputs_loaded = True
            try:
                workflow_inputs = load_workflow_inputs(repository, workflow_paths)
            except YagaError as error:
                workflow_input_error = error
        if provider in _WORKFLOW_PROVIDERS and workflow_input_error is not None:
            checks.append(RepositoryCheckResult(provider=provider, error=workflow_input_error))
            continue
        if provider in _PARSED_WORKFLOW_PROVIDERS and not workflow_inputs_parsed:
            workflow_inputs_parsed = True
            assert workflow_inputs is not None
            try:
                parsed_workflow_inputs = parse_workflow_inputs(workflow_inputs)
            except YagaError as error:
                workflow_parse_error = error
        if provider in _PARSED_WORKFLOW_PROVIDERS and workflow_parse_error is not None:
            checks.append(RepositoryCheckResult(provider=provider, error=workflow_parse_error))
            continue
        try:
            if provider is RepositoryProvider.COMMIT:
                report = check_git_commits(
                    repository,
                    config=config,
                    commit=commit,
                    revision_range=revision_range,
                )
            elif provider is RepositoryProvider.WORKFLOW:
                assert parsed_workflow_inputs is not None
                report = check_parsed_workflow_inputs(parsed_workflow_inputs)
            elif provider is RepositoryProvider.WORKFLOW_SECURITY:
                assert parsed_workflow_inputs is not None
                report = check_parsed_workflow_security_inputs(
                    parsed_workflow_inputs,
                    profile=workflow_security_profile,
                    rules=workflow_security_rules,
                )
            elif provider is RepositoryProvider.WORKFLOW_LINT:
                assert workflow_inputs is not None
                report = lint_workflow_inputs(repository, workflow_inputs)
            else:
                raise AssertionError("repository provider dispatch is incomplete")
        except YagaError as error:
            checks.append(RepositoryCheckResult(provider=provider, error=error))
        else:
            checks.append(RepositoryCheckResult(provider=provider, report=report))

    return RepositoryReport(checks=tuple(checks))


def _normalize_providers(
    providers: Sequence[str | RepositoryProvider],
) -> frozenset[RepositoryProvider]:
    if not providers:
        raise InputError("select at least one --check provider")
    if len(providers) > len(_PROVIDER_ORDER):
        raise InputError("repository check provider selection exceeds its hard limit")

    normalized: list[RepositoryProvider] = []
    for value in providers:
        try:
            provider = RepositoryProvider(value)
        except ValueError as error:
            display = safe_error_text(value, maximum=80)
            raise InputError(f"unknown repository check provider: {display}") from error
        normalized.append(provider)
    if len(set(normalized)) != len(normalized):
        raise InputError("repository check providers must not be repeated")
    return frozenset(normalized)


def _validate_provider_arguments(
    selected: frozenset[RepositoryProvider],
    *,
    config: Path | None,
    commit: str | None,
    revision_range: str | None,
    workflow_paths: Sequence[Path],
    workflow_security_profile: str | WorkflowSecurityProfile | None,
    workflow_security_rules: Sequence[str | WorkflowSecurityRule],
) -> None:
    if RepositoryProvider.COMMIT not in selected and any(
        value is not None for value in (config, commit, revision_range)
    ):
        raise InputError("commit source and --config require --check commit")
    if commit is not None and revision_range is not None:
        raise InputError("choose only one of --commit or --range")
    if not selected & _WORKFLOW_PROVIDERS and workflow_paths:
        raise InputError("--workflow-path requires a workflow check provider")
    if RepositoryProvider.WORKFLOW_SECURITY not in selected and (
        workflow_security_profile is not None or workflow_security_rules
    ):
        raise InputError("workflow security profile and rules require --check workflow-security")
