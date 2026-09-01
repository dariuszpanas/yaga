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
from yaga.workflows.parser import ParsedWorkflowInput, parse_workflow_inputs
from yaga.workflows.security_models import (
    RECOMMENDED_V1_RULES,
    RECOMMENDED_V2_RULES,
    RECOMMENDED_V3_RULES,
    WorkflowSecurityProfile,
    WorkflowSecurityReport,
    WorkflowSecurityResult,
    WorkflowSecurityRule,
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


def passing_security_report() -> WorkflowSecurityReport:
    return WorkflowSecurityReport(
        profile=WorkflowSecurityProfile.RECOMMENDED_V1,
        rules=RECOMMENDED_V1_RULES,
        results=(WorkflowSecurityResult(path="ci.yml", diagnostics=()),),
    )


@pytest.mark.parametrize(
    ("providers", "message"),
    [
        ([], "select at least one"),
        (["unknown"], "unknown repository check provider"),
        (["commit", "commit"], "must not be repeated"),
        (
            ["commit", "workflow", "workflow-security", "workflow-lint", "commit"],
            "hard limit",
        ),
    ],
)
def test_provider_selection_is_closed_explicit_and_unique(
    providers: list[str],
    message: str,
) -> None:
    with pytest.raises(InputError, match=message):
        checker.check_repository(Path("."), providers)


def test_commit_arguments_require_the_commit_provider() -> None:
    with pytest.raises(InputError, match="require the commit provider"):
        checker.check_repository(Path("."), ["workflow"], config=Path("policy.toml"))
    with pytest.raises(InputError, match="require the commit provider"):
        checker.check_repository(Path("."), ["workflow"], commit="HEAD~1")
    with pytest.raises(InputError, match="require the commit provider"):
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
    with pytest.raises(InputError, match="require the workflow-security provider"):
        checker.check_repository(
            Path("."),
            ["workflow"],
            workflow_security_profile="recommended-v1",
        )
    with pytest.raises(InputError, match="require the workflow-security provider"):
        checker.check_repository(
            Path("."),
            ["workflow"],
            workflow_security_rules=["permissions.explicit"],
        )


def test_security_selection_errors_precede_workflow_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        checker,
        "load_workflow_inputs",
        lambda *args, **kwargs: pytest.fail("invalid selection must fail before discovery"),
    )

    with pytest.raises(InputError, match="either a workflow security profile or explicit rules"):
        checker.check_repository(
            Path("."),
            ["workflow-security"],
            workflow_security_profile="recommended-v1",
            workflow_security_rules=["permissions.explicit"],
        )


@pytest.mark.parametrize(
    ("profile", "expected_profile", "expected_rules"),
    [
        (
            "recommended-v2",
            WorkflowSecurityProfile.RECOMMENDED_V2,
            RECOMMENDED_V2_RULES,
        ),
        (
            "recommended-v3",
            WorkflowSecurityProfile.RECOMMENDED_V3,
            RECOMMENDED_V3_RULES,
        ),
    ],
)
def test_recommended_profile_is_forwarded_to_the_shared_security_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    expected_profile: WorkflowSecurityProfile,
    expected_rules: tuple[WorkflowSecurityRule, ...],
) -> None:
    workflow = WorkflowInput(
        path=tmp_path / "ci.yml",
        relative_path="ci.yml",
        content=b"permissions: {}\njobs: {}\n",
    )
    parsed = parse_workflow_inputs((workflow,))

    monkeypatch.setattr(checker, "load_workflow_inputs", lambda *args, **kwargs: (workflow,))
    monkeypatch.setattr(checker, "parse_workflow_inputs", lambda inputs: parsed)

    def fake_security(
        inputs: tuple[ParsedWorkflowInput, ...],
        **kwargs: object,
    ) -> WorkflowSecurityReport:
        assert inputs == parsed
        assert kwargs == {"profile": profile, "rules": ()}
        return WorkflowSecurityReport(
            profile=expected_profile,
            rules=expected_rules,
            results=(WorkflowSecurityResult(path="ci.yml", diagnostics=()),),
        )

    monkeypatch.setattr(checker, "check_parsed_workflow_security_inputs", fake_security)

    report = checker.check_repository(
        tmp_path,
        ["workflow-security"],
        workflow_security_profile=profile,
    )

    assert report.status is RepositoryCheckStatus.PASSED
    security = report.checks[0].report
    assert isinstance(security, WorkflowSecurityReport)
    assert security.profile is expected_profile


def test_providers_execute_once_in_canonical_order_with_one_shared_workflow_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = WorkflowInput(
        path=tmp_path / "ci.yml",
        relative_path="ci.yml",
        content=b"jobs: {}\n",
    )
    parsed = parse_workflow_inputs((workflow,))
    events: list[str] = []

    def fake_commits(*args: object, **kwargs: object) -> ValidationReport:
        events.append("commit")
        assert kwargs["commit"] == "HEAD~1"
        return passing_commit_report()

    def fake_load(*args: object, **kwargs: object) -> tuple[WorkflowInput, ...]:
        events.append("load")
        return (workflow,)

    def fake_parse(inputs: tuple[WorkflowInput, ...]) -> tuple[ParsedWorkflowInput, ...]:
        events.append("parse")
        assert inputs == (workflow,)
        return parsed

    def fake_workflow(inputs: tuple[ParsedWorkflowInput, ...]) -> WorkflowReport:
        events.append("workflow")
        assert inputs == parsed
        return failing_workflow_report()

    def fake_security(
        inputs: tuple[ParsedWorkflowInput, ...],
        **kwargs: object,
    ) -> WorkflowSecurityReport:
        events.append("workflow-security")
        assert inputs == parsed
        assert kwargs == {"profile": None, "rules": ()}
        return passing_security_report()

    def fake_lint(repository: Path, inputs: tuple[WorkflowInput, ...]) -> WorkflowLintReport:
        events.append("workflow-lint")
        assert repository == tmp_path
        assert inputs == (workflow,)
        return passing_lint_report()

    monkeypatch.setattr(checker, "check_git_commits", fake_commits)
    monkeypatch.setattr(checker, "load_workflow_inputs", fake_load)
    monkeypatch.setattr(checker, "parse_workflow_inputs", fake_parse)
    monkeypatch.setattr(checker, "check_parsed_workflow_inputs", fake_workflow)
    monkeypatch.setattr(checker, "check_parsed_workflow_security_inputs", fake_security)
    monkeypatch.setattr(checker, "lint_workflow_inputs", fake_lint)

    report = checker.check_repository(
        tmp_path,
        ["workflow-lint", "workflow-security", "commit", "workflow"],
        commit="HEAD~1",
        workflow_paths=[Path("ci.yml")],
    )

    assert events == [
        "commit",
        "load",
        "parse",
        "workflow",
        "workflow-security",
        "workflow-lint",
    ]
    assert [check.provider for check in report.checks] == [
        RepositoryProvider.COMMIT,
        RepositoryProvider.WORKFLOW,
        RepositoryProvider.WORKFLOW_SECURITY,
        RepositoryProvider.WORKFLOW_LINT,
    ]
    assert [check.status for check in report.checks] == [
        RepositoryCheckStatus.PASSED,
        RepositoryCheckStatus.FAILED,
        RepositoryCheckStatus.PASSED,
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
    parsed = parse_workflow_inputs((workflow,))
    monkeypatch.setattr(
        checker,
        "check_git_commits",
        lambda *args, **kwargs: (_ for _ in ()).throw(GitError("missing history")),
    )
    monkeypatch.setattr(checker, "load_workflow_inputs", lambda *args, **kwargs: (workflow,))
    monkeypatch.setattr(checker, "parse_workflow_inputs", lambda inputs: parsed)
    monkeypatch.setattr(
        checker, "check_parsed_workflow_inputs", lambda inputs: failing_workflow_report()
    )
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
    calls = {"load": 0, "parse": 0, "workflow": 0, "security": 0, "lint": 0}
    monkeypatch.setattr(
        checker, "check_git_commits", lambda *args, **kwargs: passing_commit_report()
    )

    def fail_load(*args: object, **kwargs: object) -> tuple[WorkflowInput, ...]:
        calls["load"] += 1
        raise InputError("workflow directory missing")

    def unexpected_parse(*args: object, **kwargs: object) -> tuple[ParsedWorkflowInput, ...]:
        calls["parse"] += 1
        raise AssertionError("parse provider should not run")

    def unexpected_workflow(*args: object, **kwargs: object) -> WorkflowReport:
        calls["workflow"] += 1
        return passing_workflow_report()

    def unexpected_security(*args: object, **kwargs: object) -> WorkflowSecurityReport:
        calls["security"] += 1
        return passing_security_report()

    def unexpected_lint(*args: object, **kwargs: object) -> WorkflowLintReport:
        calls["lint"] += 1
        return passing_lint_report()

    monkeypatch.setattr(checker, "load_workflow_inputs", fail_load)
    monkeypatch.setattr(checker, "parse_workflow_inputs", unexpected_parse)
    monkeypatch.setattr(checker, "check_parsed_workflow_inputs", unexpected_workflow)
    monkeypatch.setattr(checker, "check_parsed_workflow_security_inputs", unexpected_security)
    monkeypatch.setattr(checker, "lint_workflow_inputs", unexpected_lint)

    report = checker.check_repository(
        tmp_path,
        ["workflow-lint", "workflow-security", "workflow", "commit"],
    )

    assert calls == {"load": 1, "parse": 0, "workflow": 0, "security": 0, "lint": 0}
    assert [check.status for check in report.checks] == [
        RepositoryCheckStatus.PASSED,
        RepositoryCheckStatus.ERROR,
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
    parsed = parse_workflow_inputs((workflow,))
    monkeypatch.setattr(checker, "load_workflow_inputs", lambda *args, **kwargs: (workflow,))
    monkeypatch.setattr(checker, "parse_workflow_inputs", lambda inputs: parsed)
    monkeypatch.setattr(
        checker, "check_parsed_workflow_inputs", lambda inputs: passing_workflow_report()
    )
    monkeypatch.setattr(
        checker,
        "lint_workflow_inputs",
        lambda *args, **kwargs: pytest.fail("lint provider should not run"),
    )

    report = checker.check_repository(tmp_path, ["workflow"])

    assert report.valid is True


def test_workflow_provider_checks_job_and_service_container_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "containers.yml").write_text(
        "jobs:\n"
        "  test:\n"
        "    container: python:3.12\n"
        "    services:\n"
        "      postgres:\n"
        f"        image: postgres@sha256:{'b' * 64}\n"
        "    steps: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        checker,
        "lint_workflow_inputs",
        lambda *args, **kwargs: pytest.fail("lint provider should not run"),
    )

    report = checker.check_repository(tmp_path, ["workflow"])

    assert report.status is RepositoryCheckStatus.FAILED
    result = report.checks[0]
    assert isinstance(result.report, WorkflowReport)
    assert result.report.results[0].references_checked == 2
    assert [diagnostic.code for diagnostic in result.report.results[0].diagnostics] == ["image.pin"]


def test_parse_error_is_shared_by_pure_providers_while_lint_still_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = WorkflowInput(
        path=tmp_path / "ci.yml",
        relative_path="ci.yml",
        content=b"jobs: [\n",
    )
    calls = {"parse": 0, "workflow": 0, "security": 0, "lint": 0}
    monkeypatch.setattr(checker, "load_workflow_inputs", lambda *args, **kwargs: (workflow,))

    def fail_parse(inputs: tuple[WorkflowInput, ...]) -> tuple[ParsedWorkflowInput, ...]:
        calls["parse"] += 1
        raise InputError("malformed workflow")

    def unexpected_workflow(*args: object, **kwargs: object) -> WorkflowReport:
        calls["workflow"] += 1
        return passing_workflow_report()

    def unexpected_security(*args: object, **kwargs: object) -> WorkflowSecurityReport:
        calls["security"] += 1
        return passing_security_report()

    def fake_lint(repository: Path, inputs: tuple[WorkflowInput, ...]) -> WorkflowLintReport:
        calls["lint"] += 1
        assert repository == tmp_path
        assert inputs == (workflow,)
        return passing_lint_report()

    monkeypatch.setattr(checker, "parse_workflow_inputs", fail_parse)
    monkeypatch.setattr(checker, "check_parsed_workflow_inputs", unexpected_workflow)
    monkeypatch.setattr(checker, "check_parsed_workflow_security_inputs", unexpected_security)
    monkeypatch.setattr(checker, "lint_workflow_inputs", fake_lint)

    report = checker.check_repository(
        tmp_path,
        ["workflow-lint", "workflow-security", "workflow"],
    )

    assert calls == {"parse": 1, "workflow": 0, "security": 0, "lint": 1}
    assert [check.status for check in report.checks] == [
        RepositoryCheckStatus.ERROR,
        RepositoryCheckStatus.ERROR,
        RepositoryCheckStatus.PASSED,
    ]
