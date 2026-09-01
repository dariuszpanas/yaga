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
from yaga.modes.checker import check_modes
from yaga.modes.models import ModeEntry, ModeKind, ModePolicy, ModeSelection
from yaga.modes.reporting import ModeOutputFormat, mode_report_document, render_mode_report
from yaga.modes.service import CheckedMode
from yaga.paths.checker import check_paths
from yaga.paths.models import PathPolicy, PathSelection
from yaga.paths.reporting import PathOutputFormat, path_report_document, render_path_report
from yaga.paths.service import CheckedPath
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
from yaga.sizes.models import BlobEntry, SizePolicy, SizeReport, SizeSelection
from yaga.sizes.reporting import SizeOutputFormat, render_size_report, size_report_document
from yaga.sizes.service import CheckedSize
from yaga.trees.models import TreePolicy, TreeReport, TreeSelection
from yaga.trees.reporting import TreeOutputFormat, render_tree_report, tree_report_document
from yaga.trees.service import CheckedTree
from yaga.workflows.models import (
    WorkflowDiagnostic,
    WorkflowLintReport,
    WorkflowLintResult,
    WorkflowReport,
    WorkflowResult,
)
from yaga.workflows.security_models import (
    RECOMMENDED_V1_RULES,
    RECOMMENDED_V2_RULES,
    RECOMMENDED_V3_RULES,
    WorkflowSecurityProfile,
    WorkflowSecurityReport,
    WorkflowSecurityResult,
    WorkflowSecurityRule,
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


def security_report(
    *diagnostics: WorkflowDiagnostic,
    profile: WorkflowSecurityProfile = WorkflowSecurityProfile.RECOMMENDED_V1,
    rules: tuple[WorkflowSecurityRule, ...] = RECOMMENDED_V1_RULES,
) -> WorkflowSecurityReport:
    return WorkflowSecurityReport(
        profile=profile,
        rules=rules,
        results=(
            WorkflowSecurityResult(
                path=".github/workflows/security.yml",
                diagnostics=diagnostics,
            ),
        ),
    )


def mode_check(tmp_path: Path, *, findings: int = 0) -> CheckedMode:
    entries = (
        tuple(
            ModeEntry(f"bin/run-{index:03d}", "c" * 40, "100755", "blob")
            for index in range(findings)
        )
        if findings
        else (ModeEntry("README.md", "c" * 40, "100644", "blob"),)
    )
    selection = ModeSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        entries=entries,
    )
    return CheckedMode(
        report=check_modes(ModePolicy(1, (ModeKind.REGULAR,)), selection),
        policy_path=(tmp_path / "mode-policy.toml").resolve(),
    )


def path_check(tmp_path: Path, *, findings: int = 0) -> CheckedPath:
    paths = (
        tuple(f"bad-{index:03d}?.txt" for index in range(findings)) if findings else ("README.md",)
    )
    selection = PathSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=paths,
    )
    return CheckedPath(
        report=check_paths(PathPolicy(1, rules=("windows-characters",)), selection),
        policy_path=(tmp_path / "path-policy.toml").resolve(),
    )


def size_check(tmp_path: Path) -> CheckedSize:
    selection = SizeSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        blobs=(BlobEntry("README.md", "c" * 40, "100644", 5),),
        gitlinks=(),
    )
    return CheckedSize(
        report=SizeReport(
            policy=SizePolicy(1, default_max_blob_bytes=10),
            selection=selection,
            diagnostics=(),
        ),
        policy_path=(tmp_path / "size-policy.toml").resolve(),
    )


def tree_check(tmp_path: Path) -> CheckedTree:
    selection = TreeSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=("README.md",),
    )
    return CheckedTree(
        report=TreeReport(
            policy=TreePolicy(1, required_paths=("README.md",), forbidden_patterns=()),
            selection=selection,
            diagnostics=(),
        ),
        policy_path=(tmp_path / "tree-policy.toml").resolve(),
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


def test_repository_reports_preserve_recommended_v2_selection() -> None:
    report = RepositoryReport(
        checks=(
            RepositoryCheckResult(
                provider=RepositoryProvider.WORKFLOW_SECURITY,
                report=security_report(
                    profile=WorkflowSecurityProfile.RECOMMENDED_V2,
                    rules=RECOMMENDED_V2_RULES,
                ),
            ),
        )
    )

    document = json.loads(render_repository_report(report, RepositoryOutputFormat.JSON))
    rendered = render_repository_report(report, RepositoryOutputFormat.TEXT)

    security = document["checks"][0]["report"]
    assert security["profile"] == "recommended-v2"
    assert security["rules"] == [rule.value for rule in RECOMMENDED_V2_RULES]
    assert "Profile: recommended-v2" in rendered
    assert "checkout.persist_credentials" in rendered


def test_repository_reports_preserve_recommended_v3_selection() -> None:
    report = RepositoryReport(
        checks=(
            RepositoryCheckResult(
                provider=RepositoryProvider.WORKFLOW_SECURITY,
                report=security_report(
                    WorkflowDiagnostic(
                        "security.permissions.pull_request_write",
                        (
                            "pull_request workflows must not grant pull-requests or statuses "
                            "write access"
                        ),
                        6,
                        22,
                    ),
                    profile=WorkflowSecurityProfile.RECOMMENDED_V3,
                    rules=RECOMMENDED_V3_RULES,
                ),
            ),
        )
    )

    document = json.loads(render_repository_report(report, RepositoryOutputFormat.JSON))
    rendered = render_repository_report(report, RepositoryOutputFormat.TEXT)

    security = document["checks"][0]["report"]
    assert security["profile"] == "recommended-v3"
    assert security["rules"] == [rule.value for rule in RECOMMENDED_V3_RULES]
    assert security["workflows"][0]["diagnostics"][0]["code"] == (
        "security.permissions.pull_request_write"
    )
    assert "Profile: recommended-v3" in rendered
    assert "permissions.pull_request_write" in rendered


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


def test_repository_json_embeds_standalone_committed_provider_documents_unchanged(
    tmp_path: Path,
) -> None:
    checked = (
        (RepositoryProvider.MODE, mode_check(tmp_path)),
        (RepositoryProvider.PATH, path_check(tmp_path)),
        (RepositoryProvider.SIZE, size_check(tmp_path)),
        (RepositoryProvider.TREE, tree_check(tmp_path)),
    )
    report = RepositoryReport(
        checks=tuple(
            RepositoryCheckResult(provider=provider, report=provider_report)
            for provider, provider_report in checked
        )
    )

    document = json.loads(render_repository_report(report, RepositoryOutputFormat.JSON))

    assert [item["provider"] for item in document["checks"]] == [
        "mode",
        "path",
        "size",
        "tree",
    ]
    assert document["checks"][0]["report"] == mode_report_document(checked[0][1])
    assert document["checks"][1]["report"] == path_report_document(checked[1][1])
    assert document["checks"][2]["report"] == size_report_document(checked[2][1])
    assert document["checks"][3]["report"] == tree_report_document(checked[3][1])


def test_repository_text_uses_standalone_committed_provider_text(
    tmp_path: Path,
) -> None:
    mode = mode_check(tmp_path)
    path = path_check(tmp_path)
    size = size_check(tmp_path)
    tree = tree_check(tmp_path)
    report = RepositoryReport(
        checks=(
            RepositoryCheckResult(RepositoryProvider.MODE, report=mode),
            RepositoryCheckResult(RepositoryProvider.PATH, report=path),
            RepositoryCheckResult(RepositoryProvider.SIZE, report=size),
            RepositoryCheckResult(RepositoryProvider.TREE, report=tree),
        )
    )

    rendered = render_repository_report(report, RepositoryOutputFormat.TEXT)

    assert render_mode_report(mode, ModeOutputFormat.TEXT) in rendered
    assert render_path_report(path, PathOutputFormat.TEXT) in rendered
    assert render_size_report(size, SizeOutputFormat.TEXT) in rendered
    assert render_tree_report(tree, TreeOutputFormat.TEXT) in rendered
    assert rendered.endswith("Repository checks: 4 selected; 4 passed, 0 failed, 0 errored.")


def test_repository_github_budget_counts_bounded_mode_and_path_diagnostics_exactly(
    tmp_path: Path,
) -> None:
    mode = mode_check(tmp_path, findings=300)
    path = path_check(tmp_path, findings=300)
    assert len(mode.report.diagnostics) == 256
    assert mode.report.finding_count == 300
    assert len(path.report.diagnostics) == 256
    assert path.report.finding_count == 300
    report = RepositoryReport(
        checks=(
            RepositoryCheckResult(RepositoryProvider.MODE, report=mode),
            RepositoryCheckResult(RepositoryProvider.PATH, report=path),
        )
    )

    rendered = render_repository_report(report, RepositoryOutputFormat.GITHUB)
    annotations = [line for line in rendered.splitlines() if line.startswith("::error")]

    assert len(annotations) == MAX_REPOSITORY_ANNOTATIONS
    assert all("title=YAGA mode.disallowed" in line for line in annotations[:-1])
    assert annotations[-1] == (
        "::error title=YAGA repository check::551 additional repository diagnostic(s) omitted"
    )
    assert rendered.count("::group::") == 2
    assert rendered.count("::endgroup::") == 2
    assert "292 additional diagnostic(s) omitted" in rendered
    assert rendered.endswith("YAGA repository checks: 2 selected; 0 passed, 2 failed, 0 errored.")
