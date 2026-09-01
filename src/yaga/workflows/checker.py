"""Discovery and aggregation for GitHub workflow reference policy."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from yaga.errors import InputError
from yaga.workflows.inputs import (
    WorkflowInput,
    _validate_preloaded_workflow_inputs,
    load_workflow_inputs,
)
from yaga.workflows.models import ParsedWorkflow, WorkflowReport, WorkflowResult
from yaga.workflows.parser import MAX_TOTAL_PARSED_NODES, ParsedWorkflowInput
from yaga.workflows.references import check_reference
from yaga.workflows.yaml import parse_workflow

MAX_TOTAL_NODES = MAX_TOTAL_PARSED_NODES
MAX_TOTAL_REFERENCES = 4096
MAX_TOTAL_DIAGNOSTICS = 512


def check_workflows(
    repository: Path,
    selections: Sequence[Path] = (),
) -> WorkflowReport:
    """Check selected workflow files or the default GitHub workflow directory."""
    return check_workflow_inputs(load_workflow_inputs(repository, selections))


def check_workflow_inputs(inputs: tuple[WorkflowInput, ...]) -> WorkflowReport:
    """Check one bounded tuple returned by ``load_workflow_inputs``."""
    _validate_preloaded_workflow_inputs(inputs)
    parsed_inputs = (
        (workflow.relative_path, parse_workflow(workflow.content, label=workflow.relative_path))
        for workflow in inputs
    )
    return _check_parsed_workflows(parsed_inputs)


def check_parsed_workflow_inputs(inputs: tuple[ParsedWorkflowInput, ...]) -> WorkflowReport:
    """Check one trusted tuple returned by ``parse_workflow_inputs``."""
    if not isinstance(inputs, tuple) or not inputs:
        raise InputError("parsed workflow inputs must be a nonempty tuple")
    if any(not isinstance(item, ParsedWorkflowInput) for item in inputs):
        raise InputError("parsed workflow inputs contain an invalid item")
    _validate_preloaded_workflow_inputs(tuple(item.input for item in inputs))
    return _check_parsed_workflows(
        (item.input.relative_path, item.parsed.workflow) for item in inputs
    )


def _check_parsed_workflows(
    inputs: Iterable[tuple[str, ParsedWorkflow]],
) -> WorkflowReport:
    results: list[WorkflowResult] = []
    total_nodes = 0
    total_references = 0
    total_diagnostics = 0

    for relative_path, parsed in inputs:
        total_nodes += parsed.node_count
        if total_nodes > MAX_TOTAL_NODES:
            raise InputError(f"workflow inputs exceed the hard {MAX_TOTAL_NODES}-node total limit")
        total_references += len(parsed.references)
        if total_references > MAX_TOTAL_REFERENCES:
            raise InputError(
                f"workflow inputs exceed the hard {MAX_TOTAL_REFERENCES}-reference total limit"
            )

        diagnostics = list(parsed.diagnostics)
        diagnostics.extend(
            diagnostic
            for reference in parsed.references
            if (diagnostic := check_reference(reference)) is not None
        )
        diagnostics.sort(key=lambda item: (item.line, item.column, item.code, item.message))
        total_diagnostics += len(diagnostics)
        if total_diagnostics > MAX_TOTAL_DIAGNOSTICS:
            raise InputError(
                f"workflow inputs exceed the hard {MAX_TOTAL_DIAGNOSTICS}-diagnostic total limit"
            )
        results.append(
            WorkflowResult(
                path=relative_path,
                references_checked=len(parsed.references),
                diagnostics=tuple(diagnostics),
            )
        )

    return WorkflowReport(results=tuple(results))
