"""Tests for the installed aggregate repository command."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commands import repo as repo_commands
from yaga.commits.models import CheckResult, CommitTarget, Diagnostic, ValidationReport
from yaga.errors import InputError
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
    for provider in ("commit", "workflow", "workflow-security", "workflow-lint"):
        assert provider in command_help
    assert "Repeat" in command_help
    assert "explicitly." in command_help
    assert "--workflow-security-pr" in command_help
    assert "--workflow-security-ru" in command_help
    assert "recommended-v1" in command_help
    assert "recommended-v2" in command_help
    assert "recommended-v3" in command_help


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
            "require --check commit",
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
            "require --check workflow-security",
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
