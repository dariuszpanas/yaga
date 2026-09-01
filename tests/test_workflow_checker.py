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
