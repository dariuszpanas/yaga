"""Tests for the installed aggregate repository command."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commands import repo as repo_commands
from yaga.commits.models import CheckResult, CommitTarget, Diagnostic, ValidationReport
from yaga.errors import ConfigurationError, InputError
from yaga.repository.models import (
    RepositoryCheckResult,
    RepositoryProvider,
    RepositoryReport,
)
from yaga.workflows.security_models import (
    RECOMMENDED_V2_RULES,
    RECOMMENDED_V3_RULES,
    WorkflowSecurityProfile,
    WorkflowSecurityReport,
    WorkflowSecurityResult,
    WorkflowSecurityRule,
)

runner = CliRunner()


def unstyle(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def commit_report(*diagnostics: Diagnostic) -> ValidationReport:
    return ValidationReport(
        results=(
            CheckResult(
                target=CommitTarget(label="HEAD", message="feat: aggregate checks"),
                header="feat: aggregate checks",
                diagnostics=diagnostics,
            ),
        ),
        config_path=None,
    )


def test_repo_help_exposes_closed_explicit_provider_contract() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["repo", "--help"])
    command = runner.invoke(app, ["repo", "check", "--help"])

    assert root.exit_code == 0
    assert "repo" in root.stdout
    assert group.exit_code == 0
    assert "Run explicit repository check providers" in group.stdout
    assert command.exit_code == 0
    command_help = unstyle(command.stdout)
    for provider in (
        "commit",
        "workflow",
        "workflow-security",
        "workflow-lint",
        "mode",
        "path",
        "size",
        "tree",
    ):
        assert provider in command_help
    assert "Repeat" in command_help
    assert "explicitly." in command_help
    assert "--workflow-security-pr" in command_help
    assert "--workflow-security-ru" in command_help
    assert "recommended-v1" in command_help
    assert "recommended-v2" in command_help
    assert "recommended-v3" in command_help
    assert "--plan" in command_help
    assert "--revision" in command_help
    for option in ("--mode-policy", "--path-policy", "--size-policy", "--tree-policy"):
        assert option in command_help


@pytest.mark.parametrize(
    ("source_arguments", "expected_commit", "expected_range"),
    [
        (["--commit", "HEAD~1"], "HEAD~1", None),
        (["--range", "main..HEAD"], None, "main..HEAD"),
    ],
)
def test_repo_plan_supplies_selection_and_preserves_runtime_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_arguments: list[str],
    expected_commit: str | None,
    expected_range: str | None,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    plan_path = tmp_path / "checks.toml"
    config_path = tmp_path / "commit-policy.toml"
    captured: dict[str, object] = {}

    def fake_load(path: Path) -> SimpleNamespace:
        captured["plan_path"] = path
        return SimpleNamespace(
            checks=(RepositoryProvider.COMMIT, RepositoryProvider.WORKFLOW_SECURITY),
            workflow_paths=(PurePosixPath(".github/workflows/ci.yml"),),
            workflow_security_profile=WorkflowSecurityProfile.RECOMMENDED_V3,
            workflow_security_rules=(),
            mode_policy_path=None,
            path_policy_path=None,
            size_policy_path=None,
            tree_policy_path=None,
        )

    def fake_check_repository(
        selected_repository: Path,
        providers: object,
        **kwargs: object,
    ) -> RepositoryReport:
        captured["repository"] = selected_repository
        captured["providers"] = providers
        captured.update(kwargs)
        return RepositoryReport(
            checks=(
                RepositoryCheckResult(
                    provider=RepositoryProvider.COMMIT,
                    report=commit_report(),
                ),
            )
        )

    monkeypatch.setattr(repo_commands, "load_repository_check_plan", fake_load)
    monkeypatch.setattr(repo_commands, "check_repository", fake_check_repository)

    invocation = runner.invoke(
        app,
        [
            "repo",
            "check",
            "--plan",
            str(plan_path),
            "--repo",
            str(repository),
            "--config",
            str(config_path),
            *source_arguments,
            "--format",
            "json",
        ],
    )

    assert invocation.exit_code == 0, invocation.stderr
    assert json.loads(invocation.stdout)["status"] == "passed"
    assert captured == {
        "plan_path": plan_path,
        "repository": repository,
        "providers": (
            RepositoryProvider.COMMIT,
            RepositoryProvider.WORKFLOW_SECURITY,
        ),
        "config": config_path,
        "commit": expected_commit,
        "revision_range": expected_range,
        "revision": None,
        "workflow_paths": (Path(".github/workflows/ci.yml"),),
        "workflow_security_profile": WorkflowSecurityProfile.RECOMMENDED_V3,
        "workflow_security_rules": (),
        "mode_policy_path": None,
        "path_policy_path": None,
        "size_policy_path": None,
        "tree_policy_path": None,
    }


@pytest.mark.parametrize(
    "selection_arguments",
    [
        ["--check", "commit"],
        ["--workflow-path", ".github/workflows/ci.yml"],
        ["--workflow-security-profile", "recommended-v3"],
        ["--workflow-security-rule", "permissions.explicit"],
        ["--mode-policy", ".yaga/mode-policy.toml"],
        ["--path-policy", ".yaga/path-policy.toml"],
        ["--size-policy", ".yaga/size-policy.toml"],
        ["--tree-policy", ".yaga/tree-policy.toml"],
    ],
)
def test_repo_plan_rejects_every_command_line_selection_option_before_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection_arguments: list[str],
) -> None:
    monkeypatch.setattr(
        repo_commands,
        "load_repository_check_plan",
        lambda _path: pytest.fail("mixed selection must fail before plan loading"),
    )

    result = runner.invoke(
        app,
        [
            "repo",
            "check",
            "--plan",
            str(tmp_path / "checks.toml"),
            *selection_arguments,
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    document = json.loads(result.stderr)
    assert document["error"]["message"] == (
        "--plan cannot be combined with --check, --workflow-path, "
        "--workflow-security-profile, --workflow-security-rule, --mode-policy, "
        "--path-policy, --size-policy, or --tree-policy"
    )


def test_repo_plan_load_error_uses_structured_exit_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_load(_path: Path) -> object:
        raise ConfigurationError("repository check plan is unavailable")

    monkeypatch.setattr(repo_commands, "load_repository_check_plan", fail_load)
    monkeypatch.setattr(
        repo_commands,
        "check_repository",
        lambda *args, **kwargs: pytest.fail("an invalid plan must not run providers"),
    )

    result = runner.invoke(
        app,
        [
            "repo",
            "check",
            "--plan",
            str(tmp_path / "checks.toml"),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    document = json.loads(result.stderr)
    assert document["kind"] == "repository_check"
    assert document["error"] == {
        "kind": "configuration",
        "message": "repository check plan is unavailable",
    }


def test_repo_plan_runs_real_selected_provider_with_portable_workflow_path(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "checks.toml"
    plan.write_text(
        "plan-version = 1\n"
        'checks = ["workflow-security"]\n'
        'workflow-paths = [".github/workflows/ci.yml"]\n'
        'workflow-security-profile = "recommended-v3"\n',
        encoding="utf-8",
    )
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "on: pull_request\n"
        "permissions: {}\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
        "        with:\n"
        "          persist-credentials: false\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "repo",
            "check",
            "--plan",
            str(plan),
            "--repo",
            str(tmp_path),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert [item["provider"] for item in document["checks"]] == ["workflow-security"]
    assert document["checks"][0]["report"]["profile"] == "recommended-v3"


def test_repo_plan_workflow_paths_resolve_under_repo_not_plan_directory(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    plan_directory = tmp_path / "plans" / "nested"
    plan_directory.mkdir(parents=True)
    plan = plan_directory / "checks.toml"
    plan.write_text(
        'plan-version = 1\nchecks = ["workflow"]\nworkflow-paths = ["selected/ci.yml"]\n',
        encoding="utf-8",
    )

    repository_workflow = repository / "selected" / "ci.yml"
    repository_workflow.parent.mkdir(parents=True)
    repository_workflow.write_text(
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n",
        encoding="utf-8",
    )
    plan_directory_workflow = plan_directory / "selected" / "ci.yml"
    plan_directory_workflow.parent.mkdir(parents=True)
    plan_directory_workflow.write_text(
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "repo",
            "check",
            "--plan",
            str(plan),
            "--repo",
            str(repository),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    workflow_report = document["checks"][0]["report"]
    assert workflow_report["valid"] is True
    assert workflow_report["workflows"] == [
        {
            "path": "selected/ci.yml",
            "status": "passed",
            "valid": True,
            "references_checked": 1,
            "diagnostics": [],
        }
    ]


def test_repo_plan_v2_policy_paths_resolve_under_runtime_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    plan_directory = tmp_path / "plans" / "nested"
    plan_directory.mkdir(parents=True)
    plan = plan_directory / "checks.toml"
    plan.write_text(
        "plan-version = 2\n"
        'checks = ["tree", "size", "path", "mode"]\n'
        'mode-policy = ".yaga/mode-policy.toml"\n'
        'path-policy = ".yaga/path-policy.toml"\n'
        'size-policy = ".yaga/size-policy.toml"\n'
        'tree-policy = ".yaga/tree-policy.toml"\n',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_check_repository(
        selected_repository: Path,
        providers: object,
        **kwargs: object,
    ) -> RepositoryReport:
        captured["repository"] = selected_repository
        captured["providers"] = providers
        captured.update(kwargs)
        return RepositoryReport(checks=())

    monkeypatch.setattr(repo_commands, "check_repository", fake_check_repository)

    result = runner.invoke(
        app,
        [
            "repo",
            "check",
            "--plan",
            str(plan),
            "--repo",
            str(repository),
            "--revision",
            "deadbeef",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert captured["repository"] == repository
    assert captured["providers"] == (
        RepositoryProvider.MODE,
        RepositoryProvider.PATH,
        RepositoryProvider.SIZE,
        RepositoryProvider.TREE,
    )
    assert captured["revision"] == "deadbeef"
    for provider in ("mode", "path", "size", "tree"):
        assert captured[f"{provider}_policy_path"] == (
            repository / ".yaga" / f"{provider}-policy.toml"
        )


def test_repo_plan_v2_policy_path_cannot_escape_repository_through_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    policy_directory = repository / ".yaga"
    policy_directory.mkdir(parents=True)
    outside_policy = tmp_path / "outside-mode-policy.toml"
    outside_policy.write_text("policy-version = 1\n", encoding="utf-8")
    linked_policy = policy_directory / "mode-policy.toml"
    try:
        linked_policy.symlink_to(outside_policy)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    plan = tmp_path / "checks.toml"
    plan.write_text(
        'plan-version = 2\nchecks = ["mode"]\nmode-policy = ".yaga/mode-policy.toml"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        repo_commands,
        "check_repository",
        lambda *args, **kwargs: pytest.fail("an escaping policy path must not run providers"),
    )

    result = runner.invoke(
        app,
        [
            "repo",
            "check",
            "--plan",
            str(plan),
            "--repo",
            str(repository),
            "--revision",
            "deadbeef",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"] == {
        "kind": "input",
        "message": "repository plan policy paths must resolve inside the repository",
    }


def test_repo_check_never_discovers_a_conventional_plan_implicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    plan = repository / ".yaga" / "checks" / "ci.toml"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        'plan-version = 1\nchecks = ["commit"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(repository)

    result = runner.invoke(app, ["repo", "check", "--format", "json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    document = json.loads(result.stderr)
    assert document["error"] == {
        "kind": "input",
        "message": "select at least one --check provider",
    }


def test_repo_command_uses_exit_zero_one_and_two_and_stream_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = [
        "repo",
        "check",
        "--check",
        "commit",
        "--repo",
        str(tmp_path),
        "--format",
        "json",
    ]
    monkeypatch.setattr(
        repo_commands,
        "check_repository",
        lambda *args, **kwargs: RepositoryReport(
            checks=(
                RepositoryCheckResult(
                    provider=RepositoryProvider.COMMIT,
                    report=commit_report(),
                ),
            )
        ),
    )

    passed = runner.invoke(app, arguments)
    assert passed.exit_code == 0
    assert passed.stderr == ""
    assert json.loads(passed.stdout)["status"] == "passed"

    monkeypatch.setattr(
        repo_commands,
        "check_repository",
        lambda *args, **kwargs: RepositoryReport(
            checks=(
                RepositoryCheckResult(
                    provider=RepositoryProvider.COMMIT,
                    report=commit_report(Diagnostic("syntax.header", "invalid")),
                ),
            )
        ),
    )
    failed = runner.invoke(app, arguments)
    assert failed.exit_code == 1
    assert failed.stderr == ""
    assert json.loads(failed.stdout)["status"] == "failed"

    monkeypatch.setattr(
        repo_commands,
        "check_repository",
        lambda *args, **kwargs: RepositoryReport(
            checks=(
                RepositoryCheckResult(
                    provider=RepositoryProvider.COMMIT,
                    error=InputError("repository unavailable"),
                ),
            )
        ),
    )
    errored = runner.invoke(app, arguments)
    assert errored.exit_code == 2
    assert errored.stdout == ""
    assert json.loads(errored.stderr)["status"] == "error"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ([], "select at least one"),
        (["--check", "unknown"], "unknown repository check provider"),
        (
            ["--check", "commit", "--check", "commit"],
            "providers must not be repeated",
        ),
        (
            ["--check", "workflow", "--commit", "HEAD"],
            "require the commit provider",
        ),
        (
            ["--check", "commit", "--workflow-path", "examples"],
            "requires a workflow check provider",
        ),
        (
            [
                "--check",
                "workflow",
                "--workflow-security-profile",
                "recommended-v1",
            ],
            "require the workflow-security provider",
        ),
    ],
)
def test_repo_invocation_errors_use_structured_exit_two(
    extra: list[str],
    message: str,
) -> None:
    result = runner.invoke(app, ["repo", "check", *extra, "--format", "json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    document = json.loads(result.stderr)
    assert document["kind"] == "repository_check"
    assert document["error"]["kind"] == "input"
    assert message in document["error"]["message"]


def test_repo_check_runs_real_pure_python_providers_in_canonical_order(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "YAGA Tests")
    git(repository, "config", "user.email", "yaga@example.invalid")
    message = repository / "message.txt"
    message.write_text("feat(repo): add aggregate checks", encoding="utf-8")
    git(repository, "commit", "--allow-empty", "--file", str(message))
    workflow = repository / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: CI\n"
        "on: push\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "repo",
            "check",
            "--check",
            "workflow",
            "--check",
            "workflow-security",
            "--check",
            "commit",
            "--repo",
            str(repository),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "passed"
    assert [check["provider"] for check in document["checks"]] == [
        "commit",
        "workflow",
        "workflow-security",
    ]
    assert document["checks"][0]["report"]["checked"] == 1
    assert document["checks"][1]["report"]["references_checked"] == 1
    assert document["checks"][2]["report"]["profile"] == "recommended-v1"


def test_repo_check_accepts_and_reports_recommended_v2(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "permissions: {}\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          persist-credentials: false\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "repo",
            "check",
            "--check",
            "workflow-security",
            "--repo",
            str(tmp_path),
            "--workflow-security-profile",
            "recommended-v2",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    security = document["checks"][0]["report"]
    assert security["profile"] == "recommended-v2"
    assert security["rules"] == [rule.value for rule in RECOMMENDED_V2_RULES]
    assert security["valid"] is True


def test_repo_check_accepts_and_reports_recommended_v3(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "on: pull_request\n"
        "permissions: {}\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          persist-credentials: false\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "repo",
            "check",
            "--check",
            "workflow-security",
            "--repo",
            str(tmp_path),
            "--workflow-security-profile",
            "recommended-v3",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    security = document["checks"][0]["report"]
    assert security["profile"] == "recommended-v3"
    assert security["rules"] == [rule.value for rule in RECOMMENDED_V3_RULES]
    assert security["valid"] is True


def test_repo_command_passes_exact_security_selection_to_orchestrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_check_repository(*args: object, **kwargs: object) -> RepositoryReport:
        captured.update(kwargs)
        return RepositoryReport(
            checks=(
                RepositoryCheckResult(
                    provider=RepositoryProvider.WORKFLOW_SECURITY,
                    report=WorkflowSecurityReport(
                        profile=WorkflowSecurityProfile.CUSTOM,
                        rules=(WorkflowSecurityRule.PERMISSIONS_EXPLICIT,),
                        results=(WorkflowSecurityResult(path="ci.yml", diagnostics=()),),
                    ),
                ),
            )
        )

    monkeypatch.setattr(repo_commands, "check_repository", fake_check_repository)

    result = runner.invoke(
        app,
        [
            "repo",
            "check",
            "--check",
            "workflow-security",
            "--repo",
            str(tmp_path),
            "--workflow-security-rule",
            "permissions.explicit",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert captured["workflow_security_profile"] is None
    assert captured["workflow_security_rules"] == ["permissions.explicit"]
