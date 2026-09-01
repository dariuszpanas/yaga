"""Standalone, preloaded, and parse-once workflow security services."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from yaga.errors import InputError
from yaga.workflows.inputs import (
    WorkflowInput,
    _validate_preloaded_workflow_inputs,
    load_workflow_inputs,
)
from yaga.workflows.parser import (
    MAX_TOTAL_PARSED_NODES,
    ParsedWorkflowInput,
    parse_workflow_inputs,
)
from yaga.workflows.security_facts import ParsedWorkflowBundle
from yaga.workflows.security_models import (
    WorkflowSecurityProfile,
    WorkflowSecurityReport,
    WorkflowSecurityResult,
    WorkflowSecurityRule,
    WorkflowSecuritySelection,
)
from yaga.workflows.security_rules import (
    evaluate_workflow_security,
    normalize_security_rules,
)

MAX_TOTAL_SECURITY_DIAGNOSTICS = 512


def check_workflow_security(
    repository: Path,
    selections: Sequence[Path] = (),
    *,
    profile: str | WorkflowSecurityProfile | None = None,
    rules: Sequence[str | WorkflowSecurityRule] = (),
) -> WorkflowSecurityReport:
    """Check discovered workflows with one named profile or exact custom rules."""
    selection = normalize_security_rules(profile=profile, rules=rules)
    inputs = load_workflow_inputs(repository, selections)
    return _check_parsed_workflow_security_inputs(
        parse_workflow_inputs(inputs),
        selection=selection,
    )


def check_workflow_security_inputs(
    inputs: tuple[WorkflowInput, ...],
    *,
    profile: str | WorkflowSecurityProfile | None = None,
    rules: Sequence[str | WorkflowSecurityRule] = (),
) -> WorkflowSecurityReport:
    """Check one shared bounded workflow tuple without rediscovering paths."""
    selection = normalize_security_rules(profile=profile, rules=rules)
    return _check_parsed_workflow_security_inputs(
        parse_workflow_inputs(inputs),
        selection=selection,
    )


def check_parsed_workflow_security_inputs(
    inputs: tuple[ParsedWorkflowInput, ...],
    *,
    profile: str | WorkflowSecurityProfile | None = None,
    rules: Sequence[str | WorkflowSecurityRule] = (),
) -> WorkflowSecurityReport:
    """Check a validated parse-once tuple without composing YAML again."""
    selection = normalize_security_rules(profile=profile, rules=rules)
    _validate_parsed_workflow_inputs(inputs)
    return _check_parsed_workflow_security_inputs(inputs, selection=selection)


def _check_parsed_workflow_security_inputs(
    inputs: tuple[ParsedWorkflowInput, ...],
    *,
    selection: WorkflowSecuritySelection,
) -> WorkflowSecurityReport:
    results: list[WorkflowSecurityResult] = []
    total_diagnostics = 0
    for workflow in inputs:
        diagnostics = evaluate_workflow_security(
            workflow.parsed.security,
            selection.rules,
        )
        total_diagnostics += len(diagnostics)
        if total_diagnostics > MAX_TOTAL_SECURITY_DIAGNOSTICS:
            raise InputError(
                "workflow security findings exceed the hard "
                f"{MAX_TOTAL_SECURITY_DIAGNOSTICS}-diagnostic total limit"
            )
        results.append(
            WorkflowSecurityResult(
                path=workflow.input.relative_path,
                diagnostics=diagnostics,
            )
        )
    return WorkflowSecurityReport(
        profile=selection.profile,
        rules=selection.rules,
        results=tuple(results),
    )


def _validate_parsed_workflow_inputs(inputs: tuple[ParsedWorkflowInput, ...]) -> None:
    if not isinstance(inputs, tuple) or not inputs:
        raise InputError("parsed workflow inputs must be a nonempty tuple")
    raw_inputs: list[WorkflowInput] = []
    total_nodes = 0
    for candidate in cast(tuple[Any, ...], inputs):
        if not isinstance(candidate, ParsedWorkflowInput):
            raise InputError("parsed workflow inputs contain an invalid item")
        if not isinstance(candidate.parsed, ParsedWorkflowBundle):
            raise InputError("parsed workflow input has an invalid parse bundle")
        raw_inputs.append(candidate.input)
        total_nodes += candidate.parsed.workflow.node_count
        if total_nodes > MAX_TOTAL_PARSED_NODES:
            raise InputError(
                f"parsed workflow inputs exceed the hard {MAX_TOTAL_PARSED_NODES}-node total limit"
            )
    _validate_preloaded_workflow_inputs(tuple(raw_inputs))
