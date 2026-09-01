"""Bounded, construction-free parsing of GitHub Actions workflow YAML."""

from __future__ import annotations

import yaml
from yaml.composer import ComposerError
from yaml.error import Mark
from yaml.events import AliasEvent, ScalarEvent
from yaml.loader import SafeLoader
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from yaga.errors import InputError, safe_error_text
from yaga.workflows.models import (
    ParsedWorkflow,
    ReferenceContext,
    WorkflowDiagnostic,
    WorkflowReference,
)

MAX_WORKFLOW_BYTES = 1024 * 1024
MAX_NODES = 20_000
MAX_DEPTH = 64
MAX_ANCHORS = 64
MAX_ALIASES = 64
MAX_ALIAS_NAME_LENGTH = 128
MAX_TAG_NAME_LENGTH = 128
MAX_SCALAR_LENGTH = 65_536
MAX_EXPANDED_VISITS = 50_000
MAX_REFERENCES = 512
MAX_DIAGNOSTICS = 512

type _EdgeKey = tuple[int, int, str]


class _YamlLimitError(Exception):
    """An internal, already-bounded composition failure."""


class _BoundedSafeLoader(SafeLoader):
    """Compose safe YAML nodes while bounding parser-controlled resources."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.node_count = 0
        self.anchor_count = 0
        self.alias_count = 0
        self._depth = 0
        self.edge_marks: dict[_EdgeKey, Mark] = {}
        self.explicit_tags: dict[int, tuple[str, Mark]] = {}

    def compose_node(self, parent: Node | None, index: object) -> Node:
        self._depth += 1
        try:
            if self._depth > MAX_DEPTH:
                raise _YamlLimitError(f"exceeds the hard YAML depth limit of {MAX_DEPTH}")

            event = self.peek_event()
            edge_key = self._edge_key(parent, index)
            if edge_key is not None:
                self.edge_marks[edge_key] = event.start_mark

            if isinstance(event, AliasEvent):
                self.alias_count += 1
                if self.alias_count > MAX_ALIASES:
                    raise _YamlLimitError(f"exceeds the hard alias limit of {MAX_ALIASES}")
                self._check_alias_name(event.anchor)
                return super().compose_node(parent, index)

            self.node_count += 1
            if self.node_count > MAX_NODES:
                raise _YamlLimitError(f"exceeds the hard node limit of {MAX_NODES}")

            anchor = getattr(event, "anchor", None)
            if anchor is not None:
                self.anchor_count += 1
                if self.anchor_count > MAX_ANCHORS:
                    raise _YamlLimitError(f"exceeds the hard anchor limit of {MAX_ANCHORS}")
                self._check_alias_name(anchor)

            tag = getattr(event, "tag", None)
            if tag is not None and len(tag) > MAX_TAG_NAME_LENGTH:
                raise _YamlLimitError(
                    f"contains a tag name longer than {MAX_TAG_NAME_LENGTH} characters"
                )

            if isinstance(event, ScalarEvent) and len(event.value) > MAX_SCALAR_LENGTH:
                raise _YamlLimitError(
                    f"contains a scalar longer than {MAX_SCALAR_LENGTH} characters"
                )

            node = super().compose_node(parent, index)
            if tag is not None:
                self.explicit_tags[id(node)] = (tag, event.start_mark)
            return node
        finally:
            self._depth -= 1

    def _check_alias_name(self, name: str) -> None:
        if len(name) > MAX_ALIAS_NAME_LENGTH:
            raise _YamlLimitError(
                f"contains an anchor or alias name longer than {MAX_ALIAS_NAME_LENGTH} characters"
            )

    def _edge_key(self, parent: Node | None, index: object) -> _EdgeKey | None:
        if isinstance(parent, MappingNode):
            side = "key" if index is None else "value"
            return (id(parent), len(parent.value), side)
        if isinstance(parent, SequenceNode) and isinstance(index, int):
            return (id(parent), index, "item")
        return None


class _Inspector:
    def __init__(
        self,
        *,
        label: str,
        edge_marks: dict[_EdgeKey, Mark],
        explicit_tags: dict[int, tuple[str, Mark]],
    ) -> None:
        self.label = label
        self.edge_marks = edge_marks
        self.explicit_tags = explicit_tags
        self.diagnostics: list[WorkflowDiagnostic] = []
        self.references: list[WorkflowReference] = []
        self._expanded_visits = 0
        self._active_nodes: set[int] = set()
        self._checked_nodes: set[int] = set()

    def inspect(
        self, root: Node
    ) -> tuple[tuple[WorkflowReference, ...], tuple[WorkflowDiagnostic, ...]]:
        self._validate_graph(root)
        self._extract_workflow(root)
        references = tuple(sorted(self.references, key=lambda item: (item.line, item.column)))
        diagnostics = tuple(
            sorted(
                self.diagnostics,
                key=lambda item: (item.line, item.column, item.code, item.message),
            )
        )
        return references, diagnostics

    def _validate_graph(self, node: Node) -> None:
        pending: list[tuple[Node, bool]] = [(node, False)]
        while pending:
            current, exiting = pending.pop()
            identity = id(current)
            if exiting:
                self._active_nodes.remove(identity)
                continue

            self._expanded_visits += 1
            if self._expanded_visits > MAX_EXPANDED_VISITS:
                self._fail(f"exceeds the hard expanded-node visit limit of {MAX_EXPANDED_VISITS}")
            if identity in self._active_nodes:
                self._fail("contains an alias cycle")

            self._active_nodes.add(identity)
            pending.append((current, True))
            if identity not in self._checked_nodes:
                self._checked_nodes.add(identity)
                tagged = self.explicit_tags.get(identity)
                if tagged is not None:
                    tag, mark = tagged
                    display_tag = safe_error_text(tag, maximum=80)
                    self._diagnose(
                        "yaml.tag",
                        f"explicit YAML tag is not supported: {display_tag}",
                        mark,
                    )
                if isinstance(current, MappingNode):
                    self._check_mapping_syntax(current)

            if isinstance(current, MappingNode):
                for key, value in reversed(current.value):
                    pending.append((value, False))
                    pending.append((key, False))
            elif isinstance(current, SequenceNode):
                for item in reversed(current.value):
                    pending.append((item, False))

    def _check_mapping_syntax(self, node: MappingNode) -> None:
        seen: set[tuple[str, str]] = set()
        for pair_index, (key, _) in enumerate(node.value):
            if not isinstance(key, ScalarNode):
                continue
            mark = self._edge_mark(node, pair_index, "key", key.start_mark)
            identity = (key.tag, key.value)
            if identity in seen:
                display_key = safe_error_text(key.value, maximum=80)
                self._diagnose(
                    "yaml.duplicate_key",
                    f"duplicate YAML mapping key: {display_key}",
                    mark,
                )
            else:
                seen.add(identity)
            if key.tag == "tag:yaml.org,2002:merge":
                self._diagnose(
                    "yaml.merge_key",
                    "YAML merge keys are not supported",
                    mark,
                )

    def _extract_workflow(self, root: Node) -> None:
        if not isinstance(root, MappingNode):
            self._diagnose(
                "workflow.structure",
                "workflow document root must be a mapping",
                root.start_mark,
            )
            return

        jobs: list[tuple[MappingNode, int, Node]] = []
        for pair_index, (key, value) in enumerate(root.value):
            if not isinstance(key, ScalarNode):
                self._diagnose(
                    "workflow.structure",
                    "workflow mapping keys must be scalars",
                    self._edge_mark(root, pair_index, "key", key.start_mark),
                )
                continue
            if key.value == "jobs":
                jobs.append((root, pair_index, value))

        if not jobs:
            self._diagnose(
                "workflow.structure",
                "workflow document must define a jobs mapping",
                root.start_mark,
            )
            return

        for parent, pair_index, jobs_node in jobs:
            if not isinstance(jobs_node, MappingNode):
                self._diagnose(
                    "workflow.structure",
                    "jobs must be a mapping",
                    self._edge_mark(parent, pair_index, "value", jobs_node.start_mark),
                )
                continue
            self._extract_jobs(jobs_node)

    def _extract_jobs(self, jobs: MappingNode) -> None:
        for pair_index, (job_id, job) in enumerate(jobs.value):
            if not isinstance(job_id, ScalarNode):
                self._diagnose(
                    "workflow.structure",
                    "job identifiers must be scalars",
                    self._edge_mark(jobs, pair_index, "key", job_id.start_mark),
                )
                continue
            if not isinstance(job, MappingNode):
                self._diagnose(
                    "workflow.structure",
                    "job definitions must be mappings",
                    self._edge_mark(jobs, pair_index, "value", job.start_mark),
                )
                continue
            self._extract_job(job)

    def _extract_job(self, job: MappingNode) -> None:
        for pair_index, (key, value) in enumerate(job.value):
            if not isinstance(key, ScalarNode):
                self._diagnose(
                    "workflow.structure",
                    "job mapping keys must be scalars",
                    self._edge_mark(job, pair_index, "key", key.start_mark),
                )
                continue
            if key.value == "uses":
                self._extract_reference(job, pair_index, value, ReferenceContext.JOB)
            elif key.value == "steps":
                self._extract_steps(job, pair_index, value)

    def _extract_steps(self, job: MappingNode, pair_index: int, steps: Node) -> None:
        if not isinstance(steps, SequenceNode):
            self._diagnose(
                "workflow.structure",
                "job steps must be a sequence",
                self._edge_mark(job, pair_index, "value", steps.start_mark),
            )
            return

        pending: list[SequenceNode] = [steps]
        while pending:
            group = pending.pop()
            nested_groups: list[SequenceNode] = []
            for item_index, step in enumerate(group.value):
                if not isinstance(step, MappingNode):
                    self._diagnose(
                        "workflow.structure",
                        "step definitions must be mappings",
                        self._edge_mark(group, item_index, "item", step.start_mark),
                    )
                    continue
                nested_groups.extend(self._extract_step(step))
            pending.extend(reversed(nested_groups))

    def _extract_step(self, step: MappingNode) -> list[SequenceNode]:
        nested_groups: list[SequenceNode] = []
        for pair_index, (key, value) in enumerate(step.value):
            if not isinstance(key, ScalarNode):
                self._diagnose(
                    "workflow.structure",
                    "step mapping keys must be scalars",
                    self._edge_mark(step, pair_index, "key", key.start_mark),
                )
                continue
            if key.value == "uses":
                self._extract_reference(step, pair_index, value, ReferenceContext.STEP)
            elif key.value == "parallel":
                if not isinstance(value, SequenceNode):
                    self._diagnose(
                        "workflow.structure",
                        "step parallel group must be a sequence",
                        self._edge_mark(step, pair_index, "value", value.start_mark),
                    )
                    continue
                nested_groups.append(value)
        return nested_groups

    def _extract_reference(
        self,
        parent: MappingNode,
        pair_index: int,
        value: Node,
        context: ReferenceContext,
    ) -> None:
        mark = self._edge_mark(parent, pair_index, "value", value.start_mark)
        if not isinstance(value, ScalarNode):
            self._diagnose(
                "workflow.structure",
                f"{context.value} uses value must be a scalar",
                mark,
            )
            return
        if len(self.references) >= MAX_REFERENCES:
            self._fail(f"exceeds the hard workflow reference limit of {MAX_REFERENCES}")
        line, column = _position(mark)
        self.references.append(
            WorkflowReference(
                value=value.value,
                context=context,
                line=line,
                column=column,
            )
        )

    def _diagnose(self, code: str, message: str, mark: Mark | None) -> None:
        if len(self.diagnostics) >= MAX_DIAGNOSTICS:
            self._fail(f"exceeds the hard workflow diagnostic limit of {MAX_DIAGNOSTICS}")
        line, column = _position(mark)
        self.diagnostics.append(
            WorkflowDiagnostic(code=code, message=message, line=line, column=column)
        )

    def _edge_mark(
        self,
        parent: Node,
        index: int,
        side: str,
        fallback: Mark | None,
    ) -> Mark | None:
        return self.edge_marks.get((id(parent), index, side), fallback)

    def _fail(self, reason: str) -> None:
        raise InputError(f"{self.label} {reason}")


def parse_workflow(raw: bytes, *, label: str) -> ParsedWorkflow:
    """Compose and inspect exactly one bounded workflow YAML document."""
    safe_label = safe_error_text(label, maximum=160) or "workflow input"
    if not isinstance(raw, bytes):
        raise InputError(f"{safe_label} must be provided as bytes")
    if len(raw) > MAX_WORKFLOW_BYTES:
        raise InputError(f"{safe_label} exceeds the hard {MAX_WORKFLOW_BYTES}-byte limit")
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise InputError(f"{safe_label} is not valid UTF-8") from None

    loader: _BoundedSafeLoader | None = None
    try:
        loader = _BoundedSafeLoader(text)
        root = loader.get_single_node()
        node_count = loader.node_count
        edge_marks = dict(loader.edge_marks)
        explicit_tags = dict(loader.explicit_tags)
    except _YamlLimitError as error:
        raise InputError(f"{safe_label} {error}") from None
    except yaml.YAMLError as error:
        if isinstance(error, ComposerError) and error.problem == "but found another document":
            raise InputError(
                f"{safe_label} must contain exactly one nonempty YAML document"
            ) from None
        raise InputError(f"{safe_label} contains invalid YAML") from None
    finally:
        if loader is not None:
            loader.dispose()

    if root is None or _is_empty_document(root):
        raise InputError(f"{safe_label} must contain exactly one nonempty YAML document")

    inspector = _Inspector(
        label=safe_label,
        edge_marks=edge_marks,
        explicit_tags=explicit_tags,
    )
    references, diagnostics = inspector.inspect(root)
    return ParsedWorkflow(
        references=references,
        diagnostics=diagnostics,
        node_count=node_count,
    )


def _is_empty_document(node: Node) -> bool:
    start_mark = node.start_mark
    end_mark = node.end_mark
    return (
        isinstance(node, ScalarNode)
        and node.tag == "tag:yaml.org,2002:null"
        and node.value == ""
        and start_mark is not None
        and end_mark is not None
        and start_mark.index == end_mark.index
    )


def _position(mark: Mark | None) -> tuple[int, int]:
    if mark is None:
        raise InputError("workflow YAML node is missing a source location")
    return mark.line + 1, mark.column + 1
