"""Discovery and aggregation for GitHub workflow reference policy."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yaga.errors import InputError
from yaga.workflows.inputs import (
    WorkflowInput,
    _validate_preloaded_workflow_inputs,
    load_workflow_inputs,
)
from yaga.workflows.models import WorkflowReport, WorkflowResult
from yaga.workflows.references import check_reference
from yaga.workflows.yaml import parse_workflow

MAX_TOTAL_NODES = 100_000
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
    results: list[WorkflowResult] = []
    total_nodes = 0
    total_references = 0
    total_diagnostics = 0

    for workflow in inputs:
        parsed = parse_workflow(workflow.content, label=workflow.relative_path)
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
                path=workflow.relative_path,
                references_checked=len(parsed.references),
                diagnostics=tuple(diagnostics),
            )
        )

    return WorkflowReport(results=tuple(results))
