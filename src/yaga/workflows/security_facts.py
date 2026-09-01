"""Immutable, source-located facts selected for workflow security policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from yaga.workflows.models import ParsedWorkflow


class WorkflowValueKind(StrEnum):
    """Closed YAML node shapes exposed to workflow security rules."""

    SCALAR = "scalar"
    MAPPING = "mapping"
    SEQUENCE = "sequence"


@dataclass(frozen=True, slots=True)
class LocatedScalar:
    """One bounded YAML scalar and its preferred occurrence location."""

    value: str
    tag: str
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class LocatedValue:
    """One selected YAML value without constructing an untrusted Python object."""

    kind: WorkflowValueKind
    line: int
    column: int
    scalar: LocatedScalar | None = None

    def __post_init__(self) -> None:
        if (self.kind is WorkflowValueKind.SCALAR) != (self.scalar is not None):
            raise ValueError("located scalar presence must match the YAML value kind")


@dataclass(frozen=True, slots=True)
class LocatedMappingEntry:
    """One mapping pair retained in source order, including malformed shapes."""

    key: LocatedValue
    value: LocatedValue


@dataclass(frozen=True, slots=True)
class WorkflowFieldFact:
    """One field occurrence plus every direct mapping pair when applicable."""

    value: LocatedValue
    entries: tuple[LocatedMappingEntry, ...]

    def __post_init__(self) -> None:
        if self.value.kind is not WorkflowValueKind.MAPPING and self.entries:
            raise ValueError("only a mapping field occurrence may expose mapping entries")


@dataclass(frozen=True, slots=True)
class WorkflowTriggerFact:
    """One top-level ``on`` occurrence and its selected event-name nodes."""

    value: LocatedValue
    events: tuple[LocatedValue, ...]


@dataclass(frozen=True, slots=True)
class WorkflowSecurityStep:
    """Security-relevant fields from one step, including a parallel child step."""

    line: int
    column: int
    uses: tuple[LocatedValue, ...]
    inputs: tuple[WorkflowFieldFact, ...]


@dataclass(frozen=True, slots=True)
class WorkflowSecurityJob:
    """Security-relevant fields from one workflow job occurrence."""

    identifier: LocatedScalar
    line: int
    column: int
    permissions: tuple[WorkflowFieldFact, ...]
    uses: tuple[LocatedValue, ...]
    secrets: tuple[LocatedValue, ...]
    steps: tuple[WorkflowSecurityStep, ...]


@dataclass(frozen=True, slots=True)
class WorkflowSecurityFacts:
    """Complete bounded fact selection for one composed workflow document."""

    root: LocatedValue
    triggers: tuple[WorkflowTriggerFact, ...]
    permissions: tuple[WorkflowFieldFact, ...]
    jobs: tuple[WorkflowSecurityJob, ...]


@dataclass(frozen=True, slots=True)
class ParsedWorkflowBundle:
    """Existing workflow-policy parse plus facts from the same composition."""

    workflow: ParsedWorkflow
    security: WorkflowSecurityFacts
