"""Tests for explicit aggregate repository-check orchestration."""

from __future__ import annotations

from pathlib import Path

import pytest

from yaga.commits.models import CheckResult, CommitTarget, Diagnostic, ValidationReport
from yaga.errors import GitError, InputError
from yaga.repository import checker
from yaga.repository.models import RepositoryCheckStatus, RepositoryProvider
from yaga.workflows.inputs import WorkflowInput
from yaga.workflows.models import (
    WorkflowDiagnostic,
    WorkflowLintReport,
    WorkflowLintResult,
    WorkflowReport,
    WorkflowResult,
)


def passing_commit_report() -> ValidationReport:
    return ValidationReport(
        results=(
            CheckResult(
                target=CommitTarget(label="HEAD", message="feat: aggregate checks"),
                header="feat: aggregate checks",
            ),
        ),
        config_path=None,
    )


def failing_commit_report() -> ValidationReport:
    return ValidationReport(
        results=(
            CheckResult(
                target=CommitTarget(label="HEAD", message="invalid"),
                header="invalid",
                diagnostics=(Diagnostic("syntax.header", "invalid header"),),
            ),
        ),
        config_path=None,
    )


def passing_workflow_report() -> WorkflowReport:
    return WorkflowReport(
        results=(WorkflowResult(path="ci.yml", references_checked=1, diagnostics=()),)
    )


def failing_workflow_report() -> WorkflowReport:
    return WorkflowReport(
        results=(
            WorkflowResult(
                path="ci.yml",
                references_checked=1,
                diagnostics=(WorkflowDiagnostic("uses.pin", "mutable", 2, 9),),
            ),
        )
    )


def passing_lint_report() -> WorkflowLintReport:
    return WorkflowLintReport(results=(WorkflowLintResult(path="ci.yml", diagnostics=()),))


@pytest.mark.parametrize(
    ("providers", "message"),
    [
        ([], "select at least one"),
        (["unknown"], "unknown repository check provider"),
        (["commit", "commit"], "must not be repeated"),
        (["commit", "workflow", "workflow-lint", "commit"], "hard limit"),
    ],
)
def test_provider_selection_is_closed_explicit_and_unique(
    providers: list[str],
    message: str,
) -> None:
    with pytest.raises(InputError, match=message):
        checker.check_repository(Path("."), providers)


def test_commit_arguments_require_the_commit_provider() -> None:
    with pytest.raises(InputError, match="require --check commit"):
        checker.check_repository(Path("."), ["workflow"], config=Path("policy.toml"))
    with pytest.raises(InputError, match="require --check commit"):
        checker.check_repository(Path("."), ["workflow"], commit="HEAD~1")
    with pytest.raises(InputError, match="require --check commit"):
        checker.check_repository(Path("."), ["workflow"], revision_range="main..HEAD")


def test_provider_specific_arguments_are_mutually_scoped() -> None:
    with pytest.raises(InputError, match="only one"):
        checker.check_repository(
            Path("."),
            ["commit"],
            commit="HEAD",
            revision_range="main..HEAD",
        )
    with pytest.raises(InputError, match="workflow-path"):
        checker.check_repository(
            Path("."),
            ["commit"],
            workflow_paths=[Path("examples")],
        )


def test_providers_execute_once_in_canonical_order_with_one_shared_workflow_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = WorkflowInput(
        path=tmp_path / "ci.yml",
        relative_path="ci.yml",
        content=b"jobs: {}\n",
    )
    events: list[str] = []

    def fake_commits(*args: object, **kwargs: object) -> ValidationReport:
        events.append("commit")
        assert kwargs["commit"] == "HEAD~1"
        return passing_commit_report()

    def fake_load(*args: object, **kwargs: object) -> tuple[WorkflowInput, ...]:
        events.append("load")
        return (workflow,)

    def fake_workflow(inputs: tuple[WorkflowInput, ...]) -> WorkflowReport:
        events.append("workflow")
        assert inputs == (workflow,)
        return failing_workflow_report()

    def fake_lint(repository: Path, inputs: tuple[WorkflowInput, ...]) -> WorkflowLintReport:
        events.append("workflow-lint")
        assert repository == tmp_path
        assert inputs == (workflow,)
        return passing_lint_report()

    monkeypatch.setattr(checker, "check_git_commits", fake_commits)
    monkeypatch.setattr(checker, "load_workflow_inputs", fake_load)
    monkeypatch.setattr(checker, "check_workflow_inputs", fake_workflow)
    monkeypatch.setattr(checker, "lint_workflow_inputs", fake_lint)

    report = checker.check_repository(
        tmp_path,
        ["workflow-lint", "commit", "workflow"],
        commit="HEAD~1",
        workflow_paths=[Path("ci.yml")],
    )

    assert events == ["commit", "load", "workflow", "workflow-lint"]
    assert [check.provider for check in report.checks] == [
        RepositoryProvider.COMMIT,
        RepositoryProvider.WORKFLOW,
        RepositoryProvider.WORKFLOW_LINT,
    ]
    assert [check.status for check in report.checks] == [
        RepositoryCheckStatus.PASSED,
        RepositoryCheckStatus.FAILED,
        RepositoryCheckStatus.PASSED,
    ]


def test_expected_provider_errors_do_not_hide_independent_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = WorkflowInput(
        path=tmp_path / "ci.yml",
        relative_path="ci.yml",
        content=b"jobs: {}\n",
    )
    monkeypatch.setattr(
        checker,
        "check_git_commits",
        lambda *args, **kwargs: (_ for _ in ()).throw(GitError("missing history")),
    )
    monkeypatch.setattr(checker, "load_workflow_inputs", lambda *args, **kwargs: (workflow,))
    monkeypatch.setattr(checker, "check_workflow_inputs", lambda inputs: failing_workflow_report())
    monkeypatch.setattr(
        checker, "lint_workflow_inputs", lambda repository, inputs: passing_lint_report()
    )

    report = checker.check_repository(
        tmp_path,
        ["commit", "workflow", "workflow-lint"],
    )

    assert report.errored == 1
    assert report.failed == 1
    assert report.passed == 1
    assert report.status is RepositoryCheckStatus.ERROR


def test_shared_discovery_error_marks_only_selected_workflow_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"load": 0, "workflow": 0, "lint": 0}
    monkeypatch.setattr(
        checker, "check_git_commits", lambda *args, **kwargs: passing_commit_report()
    )

    def fail_load(*args: object, **kwargs: object) -> tuple[WorkflowInput, ...]:
        calls["load"] += 1
        raise InputError("workflow directory missing")

    def unexpected_workflow(*args: object, **kwargs: object) -> WorkflowReport:
        calls["workflow"] += 1
        return passing_workflow_report()

    def unexpected_lint(*args: object, **kwargs: object) -> WorkflowLintReport:
        calls["lint"] += 1
        return passing_lint_report()

    monkeypatch.setattr(checker, "load_workflow_inputs", fail_load)
    monkeypatch.setattr(checker, "check_workflow_inputs", unexpected_workflow)
    monkeypatch.setattr(checker, "lint_workflow_inputs", unexpected_lint)

    report = checker.check_repository(
        tmp_path,
        ["workflow-lint", "workflow", "commit"],
    )

    assert calls == {"load": 1, "workflow": 0, "lint": 0}
    assert [check.status for check in report.checks] == [
        RepositoryCheckStatus.PASSED,
        RepositoryCheckStatus.ERROR,
        RepositoryCheckStatus.ERROR,
    ]


def test_docker_provider_is_never_touched_when_not_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = WorkflowInput(
        path=tmp_path / "ci.yml",
        relative_path="ci.yml",
        content=b"jobs: {}\n",
    )
    monkeypatch.setattr(checker, "load_workflow_inputs", lambda *args, **kwargs: (workflow,))
    monkeypatch.setattr(checker, "check_workflow_inputs", lambda inputs: passing_workflow_report())
    monkeypatch.setattr(
        checker,
        "lint_workflow_inputs",
        lambda *args, **kwargs: pytest.fail("lint provider should not run"),
    )

    report = checker.check_repository(tmp_path, ["workflow"])

    assert report.valid is True
