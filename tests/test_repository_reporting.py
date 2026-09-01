"""Tests for aggregate repository report models and rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yaga.commits.models import (
    CheckResult,
    CommitTarget,
    Diagnostic,
    ValidationReport,
)
from yaga.errors import InputError
from yaga.repository.models import (
    RepositoryCheckResult,
    RepositoryCheckStatus,
    RepositoryOutputFormat,
    RepositoryProvider,
    RepositoryReport,
)
from yaga.repository.reporting import (
    MAX_REPOSITORY_ANNOTATIONS,
    render_repository_error,
    render_repository_report,
)
from yaga.workflows.models import (
    WorkflowDiagnostic,
    WorkflowLintReport,
    WorkflowLintResult,
    WorkflowReport,
    WorkflowResult,
)
from yaga.workflows.security_models import (
    WORKFLOW_SECURITY_RULE_ORDER,
    WorkflowSecurityProfile,
    WorkflowSecurityReport,
    WorkflowSecurityResult,
)


def commit_report(*diagnostics: Diagnostic) -> ValidationReport:
    return ValidationReport(
        results=(
            CheckResult(
                target=CommitTarget(
                    label="HEAD\n::error::",
                    message="feat: aggregate checks",
                    sha="a" * 40,
                ),
                header="feat: aggregate checks",
                diagnostics=diagnostics,
            ),
        ),
        config_path=Path("policy.toml"),
    )


def workflow_report(*diagnostics: WorkflowDiagnostic) -> WorkflowReport:
    return WorkflowReport(
        results=(
            WorkflowResult(
                path=".github/workflows/ci%,:\r\n.yml",
                references_checked=1,
                diagnostics=diagnostics,
            ),
        )
    )


def lint_report(*diagnostics: WorkflowDiagnostic) -> WorkflowLintReport:
    return WorkflowLintReport(
        results=(
            WorkflowLintResult(
                path=".github/workflows/lint.yml",
                diagnostics=diagnostics,
            ),
        )
    )


def security_report(*diagnostics: WorkflowDiagnostic) -> WorkflowSecurityReport:
    return WorkflowSecurityReport(
        profile=WorkflowSecurityProfile.RECOMMENDED_V1,
        rules=WORKFLOW_SECURITY_RULE_ORDER,
        results=(
            WorkflowSecurityResult(
                path=".github/workflows/security.yml",
                diagnostics=diagnostics,
            ),
        ),
    )


def test_repository_models_apply_error_then_finding_precedence() -> None:
    passed = RepositoryCheckResult(
        provider=RepositoryProvider.COMMIT,
        report=commit_report(),
    )
    failed = RepositoryCheckResult(
        provider=RepositoryProvider.WORKFLOW,
        report=workflow_report(WorkflowDiagnostic("uses.pin", "mutable", 3, 9)),
    )
    errored = RepositoryCheckResult(
        provider=RepositoryProvider.WORKFLOW_LINT,
        error=InputError("Docker unavailable"),
    )
    report = RepositoryReport(checks=(passed, failed, errored))

    assert passed.status is RepositoryCheckStatus.PASSED
    assert failed.status is RepositoryCheckStatus.FAILED
    assert errored.status is RepositoryCheckStatus.ERROR
    assert report.selected == 3
    assert report.passed == 1
    assert report.failed == 1
    assert report.errored == 1
    assert report.status is RepositoryCheckStatus.ERROR
    assert report.valid is False

    with pytest.raises(ValueError, match="exactly one"):
        RepositoryCheckResult(provider=RepositoryProvider.COMMIT)
    with pytest.raises(ValueError, match="exactly one"):
        RepositoryCheckResult(
            provider=RepositoryProvider.COMMIT,
            report=commit_report(),
            error=InputError("ambiguous"),
        )


def test_repository_json_embeds_separate_bounded_provider_contracts() -> None:
    report = RepositoryReport(
        checks=(
            RepositoryCheckResult(
                provider=RepositoryProvider.COMMIT,
                report=commit_report(),
            ),
            RepositoryCheckResult(
                provider=RepositoryProvider.WORKFLOW,
                report=workflow_report(WorkflowDiagnostic("uses.pin", "mutable", 3, 9)),
            ),
            RepositoryCheckResult(
                provider=RepositoryProvider.WORKFLOW_SECURITY,
                report=security_report(),
            ),
            RepositoryCheckResult(
                provider=RepositoryProvider.WORKFLOW_LINT,
                error=InputError("unsafe\n::error:: Docker failure"),
            ),
        )
    )

    document = json.loads(render_repository_report(report, RepositoryOutputFormat.JSON))

    assert document["schema_version"] == 1
    assert document["kind"] == "repository_check"
    assert document["status"] == "error"
    assert document["valid"] is False
    assert (document["selected"], document["passed"], document["failed"], document["errored"]) == (
        4,
        2,
        1,
        1,
    )
    assert [check["provider"] for check in document["checks"]] == [
        "commit",
        "workflow",
        "workflow-security",
        "workflow-lint",
    ]
    assert document["checks"][0]["report"]["commits"][0]["sha"] == "a" * 40
    assert document["checks"][1]["report"]["kind"] == "github_workflow_policy"
    assert document["checks"][2]["report"]["kind"] == "github_workflow_security"
    assert document["checks"][2]["report"]["profile"] == "recommended-v1"
    assert document["checks"][3] == {
        "provider": "workflow-lint",
        "status": "error",
        "report": None,
        "error": {"kind": "input", "message": "unsafe?::error:: Docker failure"},
    }


def test_repository_text_keeps_provider_reports_in_separate_sections() -> None:
    report = RepositoryReport(
        checks=(
            RepositoryCheckResult(
                provider=RepositoryProvider.COMMIT,
                report=commit_report(),
            ),
            RepositoryCheckResult(
                provider=RepositoryProvider.WORKFLOW_SECURITY,
                report=security_report(),
            ),
            RepositoryCheckResult(
                provider=RepositoryProvider.WORKFLOW_LINT,
                error=InputError("Docker unavailable"),
            ),
        )
    )

    rendered = render_repository_report(report, RepositoryOutputFormat.TEXT)

    assert rendered.startswith("== commit [PASSED] ==\nPASSED")
    assert "== workflow-security [PASSED] ==\nProfile: recommended-v1" in rendered
    assert "== workflow-lint [ERROR] ==\nYAGA input error: Docker unavailable" in rendered
    assert rendered.endswith("Repository checks: 3 selected; 2 passed, 0 failed, 1 errored.")


def test_repository_github_report_balances_groups_and_uses_one_global_annotation_budget() -> None:
    commit_diagnostics = tuple(
        Diagnostic(code=f"commit.{index}", message=f"bad commit {index}") for index in range(30)
    )
    workflow_diagnostics = tuple(
        WorkflowDiagnostic(
            code=f"workflow.{index}",
            message=f"bad workflow %{index}\r\n",
            line=index + 1,
            column=2,
        )
        for index in range(30)
    )
    report = RepositoryReport(
        checks=(
            RepositoryCheckResult(
                provider=RepositoryProvider.COMMIT,
                report=commit_report(*commit_diagnostics),
            ),
            RepositoryCheckResult(
                provider=RepositoryProvider.WORKFLOW,
                report=workflow_report(*workflow_diagnostics),
            ),
            RepositoryCheckResult(
                provider=RepositoryProvider.WORKFLOW_LINT,
                report=lint_report(),
            ),
        )
    )

    rendered = render_repository_report(report, RepositoryOutputFormat.GITHUB)
    lines = rendered.splitlines()
    annotations = [line for line in lines if line.startswith("::error")]

    assert lines.count("::endgroup::") == 3
    assert sum(line.startswith("::group::") for line in lines) == 3
    assert len(annotations) == MAX_REPOSITORY_ANNOTATIONS
    assert annotations[-1] == (
        "::error title=YAGA repository check::11 additional repository diagnostic(s) omitted"
    )
    assert "file=.github/workflows/ci%25%2C%3A%0D%0A.yml" in rendered
    assert lines[-1] == "YAGA repository checks: 3 selected; 1 passed, 2 failed, 0 errored."


@pytest.mark.parametrize(
    ("output_format", "expected"),
    [
        (RepositoryOutputFormat.TEXT, "YAGA input error: bad?path"),
        (
            RepositoryOutputFormat.GITHUB,
            "::error title=YAGA repository check::YAGA input error: bad?path",
        ),
    ],
)
def test_repository_invocation_errors_are_sanitized(
    output_format: RepositoryOutputFormat,
    expected: str,
) -> None:
    assert render_repository_error(InputError("bad\npath"), output_format) == expected


def test_repository_json_invocation_error_has_a_stable_kind() -> None:
    document = json.loads(
        render_repository_error(InputError("bad\npath"), RepositoryOutputFormat.JSON)
    )

    assert document == {
        "schema_version": 1,
        "kind": "repository_check",
        "status": "error",
        "valid": False,
        "error": {"kind": "input", "message": "bad?path"},
    }
