"""Tests for bounded, source-located workflow security fact extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from yaga.errors import InputError
from yaga.workflows import parser
from yaga.workflows.inputs import WorkflowInput
from yaga.workflows.security_facts import (
    LocatedValue,
    ParsedWorkflowBundle,
    WorkflowValueKind,
)
from yaga.workflows.yaml import parse_workflow, parse_workflow_bundle


def scalar(value: LocatedValue) -> str:
    assert value.kind is WorkflowValueKind.SCALAR
    assert value.scalar is not None
    return value.scalar.value


def source_line(source: str, fragment: str) -> int:
    return source.splitlines().index(fragment) + 1


def workflow_input(repository: Path, relative_path: str, content: bytes) -> WorkflowInput:
    return WorkflowInput(
        path=repository.joinpath(*relative_path.split("/")),
        relative_path=relative_path,
        content=content,
    )


def test_bundle_preserves_duplicate_field_occurrences_and_mapping_pairs() -> None:
    raw = (
        b"on: push\n"
        b"on: [pull_request, workflow_run]\n"
        b"permissions: read-all\n"
        b"permissions:\n"
        b"  contents: read\n"
        b"  contents: write\n"
        b"jobs:\n"
        b"  build:\n"
        b"    permissions: {}\n"
        b"    permissions:\n"
        b"      statuses: write\n"
        b"    uses: owner/repository/.github/workflows/one.yml@main\n"
        b"    uses: owner/repository/.github/workflows/two.yml@main\n"
        b"    secrets: inherit\n"
        b"    secrets: {}\n"
        b"    steps:\n"
        b"      - uses: actions/checkout@main\n"
        b"        uses: ./local\n"
        b"        with: {}\n"
        b"        with:\n"
        b"          ref: first\n"
        b"          ref: second\n"
    )

    bundle = parse_workflow_bundle(raw, label="ci.yml")
    facts = bundle.security

    assert parse_workflow(raw, label="ci.yml") == bundle.workflow
    assert [[scalar(event) for event in trigger.events] for trigger in facts.triggers] == [
        ["push"],
        ["pull_request", "workflow_run"],
    ]
    assert [permission.value.kind for permission in facts.permissions] == [
        WorkflowValueKind.SCALAR,
        WorkflowValueKind.MAPPING,
    ]
    assert [scalar(entry.key) for entry in facts.permissions[1].entries] == [
        "contents",
        "contents",
    ]
    assert [scalar(entry.value) for entry in facts.permissions[1].entries] == [
        "read",
        "write",
    ]

    job = facts.jobs[0]
    assert job.identifier.value == "build"
    assert len(job.permissions) == 2
    assert [scalar(value) for value in job.uses] == [
        "owner/repository/.github/workflows/one.yml@main",
        "owner/repository/.github/workflows/two.yml@main",
    ]
    assert [value.kind for value in job.secrets] == [
        WorkflowValueKind.SCALAR,
        WorkflowValueKind.MAPPING,
    ]
    step = job.steps[0]
    assert [scalar(value) for value in step.uses] == ["actions/checkout@main", "./local"]
    assert len(step.inputs) == 2
    assert step.inputs[0].value.kind is WorkflowValueKind.MAPPING
    assert step.inputs[0].entries == ()
    assert [scalar(entry.value) for entry in step.inputs[1].entries] == ["first", "second"]


def test_security_facts_retain_malformed_selected_shapes_without_construction() -> None:
    raw = (
        b"on:\n"
        b"  - pull_request_target\n"
        b"  - [nested]\n"
        b"permissions: [write-all]\n"
        b"jobs:\n"
        b"  build:\n"
        b"    permissions: [contents]\n"
        b"    uses: [not-scalar]\n"
        b"    secrets: {token: secret}\n"
        b"    steps:\n"
        b"      - uses: {bad: shape}\n"
        b"        with:\n"
        b"          ref: candidate\n"
        b"          ? [complex-key]\n"
        b"          : [complex-value]\n"
        b"        with: [not-a-mapping]\n"
    )

    bundle = parse_workflow_bundle(raw, label="ci.yml")
    facts = bundle.security

    assert [event.kind for event in facts.triggers[0].events] == [
        WorkflowValueKind.SCALAR,
        WorkflowValueKind.SEQUENCE,
    ]
    assert facts.permissions[0].value.kind is WorkflowValueKind.SEQUENCE
    job = facts.jobs[0]
    assert job.permissions[0].value.kind is WorkflowValueKind.SEQUENCE
    assert job.uses[0].kind is WorkflowValueKind.SEQUENCE
    assert job.secrets[0].kind is WorkflowValueKind.MAPPING
    step = job.steps[0]
    assert step.uses[0].kind is WorkflowValueKind.MAPPING
    assert [item.value.kind for item in step.inputs] == [
        WorkflowValueKind.MAPPING,
        WorkflowValueKind.SEQUENCE,
    ]
    malformed_entry = step.inputs[0].entries[1]
    assert malformed_entry.key.kind is WorkflowValueKind.SEQUENCE
    assert malformed_entry.value.kind is WorkflowValueKind.SEQUENCE
    assert {diagnostic.code for diagnostic in bundle.workflow.diagnostics} == {
        "workflow.structure",
        "yaml.duplicate_key",
    }


def test_alias_facts_use_the_alias_occurrence_location() -> None:
    source = (
        "event-list: &event-list [pull_request_target]\n"
        "permission-map: &permission-map\n"
        "  contents: write\n"
        "checkout-step: &checkout-step\n"
        "  uses: actions/checkout@main\n"
        "  with:\n"
        "    persist-credentials: true\n"
        "on: *event-list\n"
        "permissions: *permission-map\n"
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - *checkout-step\n"
    )

    facts = parse_workflow_bundle(source.encode(), label="ci.yml").security

    event = facts.triggers[0].events[0]
    permission = facts.permissions[0].entries[0]
    step = facts.jobs[0].steps[0]
    assert event.line == source_line(source, "on: *event-list")
    assert permission.key.line == source_line(source, "permissions: *permission-map")
    assert permission.value.line == permission.key.line
    assert step.line == source_line(source, "      - *checkout-step")
    assert step.uses[0].line == step.line
    assert step.inputs[0].entries[0].key.line == step.line


def test_parallel_child_steps_are_flattened_in_source_order() -> None:
    raw = (
        b"jobs:\n"
        b"  build:\n"
        b"    steps:\n"
        b"      - uses: owner/outer@main\n"
        b"        parallel:\n"
        b"          - uses: owner/inner-one@main\n"
        b"          - uses: owner/inner-two@main\n"
        b"      - uses: owner/final@main\n"
    )

    steps = parse_workflow_bundle(raw, label="ci.yml").security.jobs[0].steps

    assert [scalar(step.uses[0]) for step in steps] == [
        "owner/outer@main",
        "owner/inner-one@main",
        "owner/inner-two@main",
        "owner/final@main",
    ]


def test_parse_workflow_inputs_validates_and_preserves_the_exact_input_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    inputs = (
        workflow_input(repository, ".github/workflows/one.yml", b"jobs: {}\n"),
        workflow_input(repository, ".github/workflows/two.yml", b"jobs: {}\n"),
    )

    calls: list[str] = []
    parse_bundle = parser.parse_workflow_bundle

    def record_parse(raw: bytes, *, label: str) -> ParsedWorkflowBundle:
        calls.append(label)
        return parse_bundle(raw, label=label)

    monkeypatch.setattr(parser, "parse_workflow_bundle", record_parse)

    parsed = parser.parse_workflow_inputs(inputs)

    assert tuple(item.input for item in parsed) == inputs
    assert calls == [".github/workflows/one.yml", ".github/workflows/two.yml"]
    assert [item.parsed.workflow.node_count for item in parsed] == [3, 3]
    assert all(item.parsed.security.root.kind is WorkflowValueKind.MAPPING for item in parsed)


def test_parse_workflow_inputs_enforces_validation_before_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unresolved = WorkflowInput(
        path=Path("relative.yml"),
        relative_path="relative.yml",
        content=b"jobs: {}\n",
    )
    monkeypatch.setattr(
        parser,
        "parse_workflow_bundle",
        lambda *args, **kwargs: pytest.fail("invalid input must not reach composition"),
    )

    with pytest.raises(InputError, match="absolute and resolved"):
        parser.parse_workflow_inputs((unresolved,))


def test_parse_workflow_inputs_enforces_the_cross_file_node_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    inputs = (
        workflow_input(repository, "one.yml", b"jobs: {}\n"),
        workflow_input(repository, "two.yml", b"jobs: {}\n"),
    )
    monkeypatch.setattr(parser, "MAX_TOTAL_PARSED_NODES", 5)

    with pytest.raises(InputError, match="5-node total limit"):
        parser.parse_workflow_inputs(inputs)
