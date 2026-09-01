"""Tests for workflow discovery and aggregate policy checking."""

from __future__ import annotations

from pathlib import Path

import pytest

from yaga.errors import InputError
from yaga.workflows import checker
from yaga.workflows.checker import check_workflows
from yaga.workflows.models import (
    ParsedWorkflow,
    ReferenceContext,
    WorkflowDiagnostic,
    WorkflowReference,
)

PIN = "a" * 40


def write_workflow(path: Path, reference: str = f"actions/checkout@{PIN}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"name: CI\non: push\njobs:\n  check:\n    runs-on: ubuntu-latest\n"
        f"    steps:\n      - uses: {reference}\n",
        encoding="utf-8",
    )


def test_default_discovery_is_sorted_and_checks_only_direct_workflow_files(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    write_workflow(workflows / "z.yaml")
    write_workflow(workflows / "a.yml")
    write_workflow(workflows / "nested" / "ignored.yml", "actions/checkout@main")
    (workflows / "notes.txt").write_text("ignored", encoding="utf-8")

    report = check_workflows(tmp_path)

    assert [result.path for result in report.results] == [
        ".github/workflows/a.yml",
        ".github/workflows/z.yaml",
    ]
    assert report.checked == 2
    assert report.passed == 2
    assert report.references_checked == 2


def test_explicit_files_and_directories_are_deduplicated(tmp_path: Path) -> None:
    workflows = tmp_path / "examples"
    first = workflows / "first.yml"
    second = workflows / "second.yaml"
    write_workflow(first)
    write_workflow(second)

    report = check_workflows(tmp_path, [Path("examples"), first, Path("examples/first.yml")])

    assert [result.path for result in report.results] == [
        "examples/first.yml",
        "examples/second.yaml",
    ]


def test_reference_diagnostics_are_aggregated_and_sorted(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    write_workflow(workflow, "actions/checkout@main")

    report = check_workflows(tmp_path)

    assert report.valid is False
    assert report.failed == 1
    assert report.results[0].diagnostics[0].code == "uses.pin"


def test_mutable_references_inside_parallel_steps_fail_policy(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  check:\n    steps:\n      - parallel:\n          - uses: actions/checkout@main\n",
        encoding="utf-8",
    )

    report = check_workflows(tmp_path)

    assert report.valid is False
    assert report.references_checked == 1
    assert [item.code for item in report.results[0].diagnostics] == ["uses.pin"]


def test_pinned_references_inside_parallel_steps_pass_policy(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n"
        "  check:\n"
        "    steps:\n"
        "      - parallel:\n"
        f"          - uses: actions/checkout@{PIN}\n",
        encoding="utf-8",
    )

    report = check_workflows(tmp_path)

    assert report.valid is True
    assert report.references_checked == 1
    assert report.results[0].diagnostics == ()


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        (Path("missing"), "does not exist"),
        (Path("empty"), "contains no"),
        (Path("workflow.txt"), "extension"),
    ],
)
def test_invalid_or_empty_selections_fail_loudly(
    tmp_path: Path,
    selection: Path,
    message: str,
) -> None:
    target = tmp_path / selection
    if selection.name == "empty":
        target.mkdir()
    elif selection.name == "workflow.txt":
        target.write_text("name: ignored\n", encoding="utf-8")

    with pytest.raises(InputError, match=message):
        check_workflows(tmp_path, [selection])


def test_selection_cannot_escape_the_repository(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.yml"
    write_workflow(outside)

    with pytest.raises(InputError, match="inside the repository"):
        check_workflows(tmp_path, [outside])


def test_selection_errors_bound_and_sanitize_displayed_paths(tmp_path: Path) -> None:
    selection = Path("missing\n::error::" + "x" * 500)

    with pytest.raises(InputError) as caught:
        check_workflows(tmp_path, [selection])

    message = str(caught.value)
    assert "\n" not in message
    assert len(message) < 240


def test_file_and_byte_totals_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    write_workflow(workflows / "one.yml")
    write_workflow(workflows / "two.yml")

    monkeypatch.setattr(checker, "MAX_WORKFLOW_FILES", 1)
    with pytest.raises(InputError, match="file limit"):
        check_workflows(tmp_path)

    monkeypatch.setattr(checker, "MAX_WORKFLOW_FILES", 128)
    monkeypatch.setattr(checker, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(InputError, match="byte total"):
        check_workflows(tmp_path)


def test_node_totals_are_bounded_across_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    write_workflow(workflows / "one.yml")
    write_workflow(workflows / "two.yml")
    monkeypatch.setattr(
        checker,
        "parse_workflow",
        lambda raw, *, label: ParsedWorkflow(references=(), diagnostics=(), node_count=2),
    )
    monkeypatch.setattr(checker, "MAX_TOTAL_NODES", 3)

    with pytest.raises(InputError, match="node total"):
        check_workflows(tmp_path)


def test_reference_totals_are_bounded_across_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    write_workflow(workflows / "one.yml")
    write_workflow(workflows / "two.yml")
    references = tuple(
        WorkflowReference(
            value=f"owner/action@{'a' * 40}",
            context=ReferenceContext.STEP,
            line=index,
            column=1,
        )
        for index in (1, 2)
    )
    monkeypatch.setattr(
        checker,
        "parse_workflow",
        lambda raw, *, label: ParsedWorkflow(
            references=references,
            diagnostics=(),
            node_count=1,
        ),
    )
    monkeypatch.setattr(checker, "MAX_TOTAL_REFERENCES", 3)

    with pytest.raises(InputError, match="reference total"):
        check_workflows(tmp_path)


def test_diagnostic_totals_are_bounded_across_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    write_workflow(workflows / "one.yml")
    write_workflow(workflows / "two.yml")
    diagnostics = tuple(
        WorkflowDiagnostic(
            code="workflow.structure",
            message="invalid workflow structure",
            line=index,
            column=1,
        )
        for index in (1, 2)
    )
    monkeypatch.setattr(
        checker,
        "parse_workflow",
        lambda raw, *, label: ParsedWorkflow(
            references=(),
            diagnostics=diagnostics,
            node_count=1,
        ),
    )
    monkeypatch.setattr(checker, "MAX_TOTAL_DIAGNOSTICS", 3)

    with pytest.raises(InputError, match="diagnostic total"):
        check_workflows(tmp_path)


def test_directory_entry_enumeration_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    for index in range(3):
        (workflows / f"note-{index}.txt").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(checker, "MAX_DIRECTORY_ENTRIES", 2)

    with pytest.raises(InputError, match="entry limit"):
        check_workflows(tmp_path)
