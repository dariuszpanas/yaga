from __future__ import annotations

import traceback

import pytest

from yaga.errors import InputError
from yaga.workflows import yaml as workflow_yaml
from yaga.workflows.models import ReferenceContext
from yaga.workflows.yaml import parse_workflow


def test_extracts_only_step_and_job_uses_and_accepts_yaml_11_on_key() -> None:
    parsed = parse_workflow(
        b"""\
name: CI
on: [push]
uses: ignored/root@v1
jobs:
  build:
    env:
      uses: ignored/env@v1
    steps:
      - uses: actions/checkout@0123456789abcdef
      - uses: ./.github/actions/local
      - uses: docker://ghcr.io/example/check:1
  shared:
    uses: owner/repository/.github/workflows/check.yml@main
    with:
      uses: ignored/input@v1
""",
        label="workflow.yml",
    )

    assert [(reference.context, reference.value) for reference in parsed.references] == [
        (ReferenceContext.STEP, "actions/checkout@0123456789abcdef"),
        (ReferenceContext.STEP, "./.github/actions/local"),
        (ReferenceContext.STEP, "docker://ghcr.io/example/check:1"),
        (
            ReferenceContext.JOB,
            "owner/repository/.github/workflows/check.yml@main",
        ),
    ]
    assert parsed.diagnostics == ()
    assert parsed.node_count > len(parsed.references)


def test_preserves_duplicate_pairs_and_one_based_value_marks() -> None:
    parsed = parse_workflow(
        b"""\
jobs:
  build:
    steps:
      - uses: first/action@v1
        uses: second/action@v2
""",
        label="workflow.yml",
    )

    assert [reference.value for reference in parsed.references] == [
        "first/action@v1",
        "second/action@v2",
    ]
    assert [(reference.line, reference.column) for reference in parsed.references] == [
        (4, 15),
        (5, 15),
    ]
    assert [
        (diagnostic.code, diagnostic.line, diagnostic.column) for diagnostic in parsed.diagnostics
    ] == [("yaml.duplicate_key", 5, 9)]
    assert parsed.node_count == 12


def test_diagnoses_merge_keys_and_explicit_tags_without_constructing_them() -> None:
    parsed = parse_workflow(
        b"""\
defaults: &defaults
  timeout-minutes: 5
jobs:
  build:
    <<: *defaults
    uses: !custom owner/repository@ref
""",
        label="workflow.yml",
    )

    assert [reference.value for reference in parsed.references] == ["owner/repository@ref"]
    assert [
        (diagnostic.code, diagnostic.line, diagnostic.column) for diagnostic in parsed.diagnostics
    ] == [
        ("yaml.merge_key", 5, 5),
        ("yaml.tag", 6, 11),
    ]


def test_quoted_merge_spelling_is_an_ordinary_string_key() -> None:
    parsed = parse_workflow(
        b'jobs:\n  build:\n    "<<": ordinary\n    steps: []\n',
        label="workflow.yml",
    )

    assert all(diagnostic.code != "yaml.merge_key" for diagnostic in parsed.diagnostics)


def test_alias_reference_uses_the_alias_occurrence_mark() -> None:
    parsed = parse_workflow(
        b"""\
reference: &reference owner/repository@ref
jobs:
  build:
    steps:
      - uses: *reference
""",
        label="workflow.yml",
    )

    assert [(reference.line, reference.column) for reference in parsed.references] == [(5, 15)]


def test_accepts_a_utf8_bom() -> None:
    parsed = parse_workflow(
        b"\xef\xbb\xbfjobs:\n  build:\n    uses: owner/repository@ref\n",
        label="workflow.yml",
    )

    assert [reference.value for reference in parsed.references] == ["owner/repository@ref"]


@pytest.mark.parametrize("raw", [b"", b" \n# comment only\n", b"---\n"])
def test_rejects_empty_documents(raw: bytes) -> None:
    with pytest.raises(InputError, match="exactly one nonempty YAML document"):
        parse_workflow(raw, label="workflow.yml")


def test_rejects_multiple_documents() -> None:
    with pytest.raises(InputError, match="exactly one nonempty YAML document"):
        parse_workflow(b"jobs: {}\n---\njobs: {}\n", label="workflow.yml")


def test_rejects_malformed_yaml_without_echoing_source() -> None:
    raw = b"jobs: [" + b"attacker-text" + b"\n"
    with pytest.raises(InputError, match="contains invalid YAML") as caught:
        parse_workflow(raw, label="workflow.yml")

    assert "attacker-text" not in str(caught.value)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "attacker-text" not in rendered
    assert caught.value.__suppress_context__ is True


def test_rejects_invalid_utf8() -> None:
    with pytest.raises(InputError, match="not valid UTF-8"):
        parse_workflow(b"jobs: {}\n\xff", label="workflow.yml")


def test_rejects_an_oversized_document(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_yaml, "MAX_WORKFLOW_BYTES", 8)

    with pytest.raises(InputError, match="hard 8-byte limit"):
        parse_workflow(b"jobs: {}\n", label="workflow.yml")


def test_rejects_too_many_non_alias_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_yaml, "MAX_NODES", 4)

    with pytest.raises(InputError, match="node limit of 4"):
        parse_workflow(b"jobs: {build: {}}\n", label="workflow.yml")


def test_rejects_excessive_composition_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_yaml, "MAX_DEPTH", 3)

    with pytest.raises(InputError, match="depth limit of 3"):
        parse_workflow(b"jobs:\n  build:\n    steps: []\n", label="workflow.yml")


def test_rejects_an_oversized_scalar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_yaml, "MAX_SCALAR_LENGTH", 4)

    with pytest.raises(InputError, match="scalar longer than 4"):
        parse_workflow(b"jobs: {x: abcde}\n", label="workflow.yml")


def test_rejects_too_many_anchors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_yaml, "MAX_ANCHORS", 1)

    with pytest.raises(InputError, match="anchor limit of 1"):
        parse_workflow(
            b"first: &one x\nsecond: &two y\njobs: {}\n",
            label="workflow.yml",
        )


def test_rejects_too_many_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_yaml, "MAX_ALIASES", 1)

    with pytest.raises(InputError, match="alias limit of 1"):
        parse_workflow(
            b"base: &base []\nfirst: *base\nsecond: *base\njobs: {}\n",
            label="workflow.yml",
        )


def test_rejects_long_anchor_and_alias_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_yaml, "MAX_ALIAS_NAME_LENGTH", 3)

    with pytest.raises(InputError, match="anchor or alias name longer than 3"):
        parse_workflow(b"value: &long x\njobs: {}\n", label="workflow.yml")


def test_rejects_long_tag_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_yaml, "MAX_TAG_NAME_LENGTH", 3)

    with pytest.raises(InputError, match="tag name longer than 3"):
        parse_workflow(b"value: !long x\njobs: {}\n", label="workflow.yml")


def test_rejects_excessive_alias_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_yaml, "MAX_EXPANDED_VISITS", 10)

    with pytest.raises(InputError, match="expanded-node visit limit of 10"):
        parse_workflow(
            b"base: &base [x]\ncopies: [*base, *base]\njobs: {}\n",
            label="workflow.yml",
        )


def test_deep_alias_expansion_does_not_depend_on_python_recursion() -> None:
    lines = ["layer0: &layer0 value"]
    for index in range(1, 64):
        nested = "[" * 20 + f"*layer{index - 1}" + "]" * 20
        lines.append(f"layer{index}: &layer{index} {nested}")
    lines.append("jobs: {}")

    parsed = parse_workflow("\n".join(lines).encode(), label="workflow.yml")

    assert parsed.references == ()
    assert parsed.diagnostics == ()


def test_rejects_alias_cycles() -> None:
    with pytest.raises(InputError, match="alias cycle"):
        parse_workflow(
            b"jobs: &jobs\n  build:\n    steps: *jobs\n",
            label="workflow.yml",
        )


def test_rejects_too_many_references(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_yaml, "MAX_REFERENCES", 1)

    with pytest.raises(InputError, match="reference limit of 1"):
        parse_workflow(
            b"""\
jobs:
  build:
    steps:
      - uses: first/action@v1
      - uses: second/action@v2
""",
            label="workflow.yml",
        )


def test_rejects_too_many_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_yaml, "MAX_DIAGNOSTICS", 1)

    with pytest.raises(InputError, match="diagnostic limit of 1"):
        parse_workflow(
            b"""\
jobs:
  build:
    uses: first/action@v1
    uses: second/action@v2
    uses: third/action@v3
""",
            label="workflow.yml",
        )


@pytest.mark.parametrize(
    ("raw", "message", "line", "column"),
    [
        (b"[]\n", "root must be a mapping", 1, 1),
        (b"name: CI\n", "must define a jobs mapping", 1, 1),
        (b"jobs: []\n", "jobs must be a mapping", 1, 7),
        (b"jobs:\n  build: value\n", "job definitions must be mappings", 2, 10),
        (b"jobs:\n  build:\n    steps: {}\n", "job steps must be a sequence", 3, 12),
        (
            b"jobs:\n  build:\n    steps:\n      - value\n",
            "step definitions must be mappings",
            4,
            9,
        ),
        (b"jobs:\n  build:\n    uses: {}\n", "job uses value must be a scalar", 3, 11),
        (
            b"jobs:\n  build:\n    steps:\n      - uses: []\n",
            "step uses value must be a scalar",
            4,
            15,
        ),
    ],
)
def test_reports_unsupported_workflow_structures(
    raw: bytes,
    message: str,
    line: int,
    column: int,
) -> None:
    parsed = parse_workflow(raw, label="workflow.yml")

    matching = [
        diagnostic
        for diagnostic in parsed.diagnostics
        if diagnostic.code == "workflow.structure" and message in diagnostic.message
    ]
    assert [(diagnostic.line, diagnostic.column) for diagnostic in matching] == [(line, column)]


def test_extracts_action_references_from_parallel_step_groups() -> None:
    parsed = parse_workflow(
        b"""\
jobs:
  build:
    steps:
      - parallel:
          - uses: actions/checkout@main
          - parallel:
              - uses: owner/action@0123456789abcdef0123456789abcdef01234567
      - uses: ./local
""",
        label="workflow.yml",
    )

    assert [reference.value for reference in parsed.references] == [
        "actions/checkout@main",
        "owner/action@0123456789abcdef0123456789abcdef01234567",
        "./local",
    ]
    assert all(reference.context is ReferenceContext.STEP for reference in parsed.references)


def test_reports_invalid_parallel_step_group_structure() -> None:
    parsed = parse_workflow(
        b"jobs:\n"
        b"  build:\n"
        b"    steps:\n"
        b"      - parallel:\n"
        b"          - invalid\n"
        b"      - parallel: {}\n",
        label="workflow.yml",
    )

    matching = [
        diagnostic for diagnostic in parsed.diagnostics if diagnostic.code == "workflow.structure"
    ]
    assert [
        (diagnostic.message, diagnostic.line, diagnostic.column) for diagnostic in matching
    ] == [
        ("step definitions must be mappings", 5, 13),
        ("step parallel group must be a sequence", 6, 19),
    ]


def test_bounds_and_sanitizes_the_display_label() -> None:
    with pytest.raises(InputError) as caught:
        parse_workflow(b"", label="bad\n::error::" + "x" * 500)

    message = str(caught.value)
    assert "\n" not in message
    assert len(message) < 240
