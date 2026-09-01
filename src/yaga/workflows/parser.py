"""Validated parse-once batches for installed workflow providers."""

from __future__ import annotations

from dataclasses import dataclass

from yaga.errors import InputError
from yaga.workflows.inputs import WorkflowInput, _validate_preloaded_workflow_inputs
from yaga.workflows.security_facts import ParsedWorkflowBundle
from yaga.workflows.yaml import parse_workflow_bundle

MAX_TOTAL_PARSED_NODES = 100_000


@dataclass(frozen=True, slots=True)
class ParsedWorkflowInput:
    """One validated workflow input and the facts composed from its exact bytes."""

    input: WorkflowInput
    parsed: ParsedWorkflowBundle


def parse_workflow_inputs(
    inputs: tuple[WorkflowInput, ...],
) -> tuple[ParsedWorkflowInput, ...]:
    """Compose one validated input tuple once under the cross-file node bound."""
    _validate_preloaded_workflow_inputs(inputs)
    parsed_inputs: list[ParsedWorkflowInput] = []
    total_nodes = 0
    for workflow in inputs:
        parsed = parse_workflow_bundle(
            workflow.content,
            label=workflow.relative_path,
        )
        total_nodes += parsed.workflow.node_count
        if total_nodes > MAX_TOTAL_PARSED_NODES:
            raise InputError(
                f"workflow inputs exceed the hard {MAX_TOTAL_PARSED_NODES}-node total limit"
            )
        parsed_inputs.append(ParsedWorkflowInput(input=workflow, parsed=parsed))
    return tuple(parsed_inputs)
