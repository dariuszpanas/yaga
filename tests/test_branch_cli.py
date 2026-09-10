"""Tests for the installed explicit branch-policy command."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.branches.checker import check_branch_name
from yaga.branches.models import BranchPolicy
from yaga.branches.service import CheckedBranch
from yaga.cli import app
from yaga.commands import branch as branch_commands
from yaga.errors import InputError

runner = CliRunner()


def _unstyle(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def _checked(tmp_path: Path, branch: str) -> CheckedBranch:
    policy = BranchPolicy(branch_policy_version=1, allowed_patterns=("main", "feat/*"))
    return CheckedBranch(
        report=check_branch_name(policy, branch),
        policy_path=(tmp_path / "branch-policy.toml").resolve(),
    )


def test_branch_help_exposes_only_explicit_policy_name_and_format() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["branch", "--help"])
    command = runner.invoke(app, ["branch", "check", "--help"])

    assert root.exit_code == 0
    assert "branch" in root.stdout
    assert group.exit_code == 0
    assert "Check branch-name policy" in group.stdout
    assert command.exit_code == 0
    help_text = _unstyle(command.stdout)
    for option in ("--policy", "--name", "--format"):
        assert option in help_text
    for output_format in ("text", "json", "github"):
        assert output_format in help_text
    assert "--quiet" in help_text
    for unsupported in ("--repo", "--current", "--event-file", "--token"):
        assert unsupported not in help_text


@pytest.mark.parametrize("missing", ["policy", "name"])
def test_branch_check_requires_both_explicit_inputs(missing: str) -> None:
    arguments = ["branch", "check"]
    if missing != "policy":
        arguments.extend(("--policy", "branch-policy.toml"))
    if missing != "name":
        arguments.extend(("--name", "feat/topic"))

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert f"--{missing}" in _unstyle(result.stderr)


@pytest.mark.parametrize(
    ("branch", "expected_exit", "status", "quiet"),
    [
        ("feat/installed-layer", 0, "passed", False),
        ("chore/installed-layer", 1, "failed", False),
        ("feat/installed-layer", 0, "passed", True),
        ("chore/installed-layer", 1, "failed", True),
    ],
)
def test_branch_check_reports_policy_outcomes_on_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    expected_exit: int,
    status: str,
    quiet: bool,
) -> None:
    checked = _checked(tmp_path, branch)
    captured: dict[str, object] = {}

    def fake_check_branch(*, policy_path: Path, branch: str) -> CheckedBranch:
        captured.update(policy_path=policy_path, branch=branch)
        return checked

    monkeypatch.setattr(branch_commands, "check_branch", fake_check_branch)
    result = runner.invoke(
        app,
        [
            "branch",
            "check",
            "--policy",
            "policy.toml",
            "--name",
            branch,
            "--format",
            "json",
            *(["--quiet"] if quiet else []),
        ],
    )

    assert result.exit_code == expected_exit, result.stderr
    assert result.stderr == ""
    if quiet:
        assert result.stdout == ""
    else:
        assert json.loads(result.stdout)["status"] == status
    assert captured == {"policy_path": Path("policy.toml"), "branch": branch}


@pytest.mark.parametrize("output_format", ["text", "json", "github"])
def test_branch_check_renders_operational_errors_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(
        branch_commands,
        "check_branch",
        lambda **kwargs: (_ for _ in ()).throw(InputError("bad%\x1b[31m\nname")),
    )

    result = runner.invoke(
        app,
        [
            "branch",
            "check",
            "--policy",
            "policy.toml",
            "--name",
            "feat/topic",
            "--format",
            output_format,
            "--quiet",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "\x1b" not in result.stderr
    if output_format == "json":
        document = json.loads(result.stderr)
        assert document["status"] == "error"
        assert document["error"] == {"kind": "input", "message": "bad%?[31m?name"}
    elif output_format == "github":
        assert result.stderr.strip() == (
            "::error title=YAGA branch policy::YAGA input error: bad%25?[31m?name"
        )
    else:
        assert result.stderr.strip() == "YAGA input error: bad%?[31m?name"


def test_branch_check_runs_outside_a_git_repository(tmp_path: Path) -> None:
    policy = tmp_path / "branch-policy.toml"
    policy.write_text(
        'branch-policy-version = 1\nallowed-patterns = ["main", "feat/*"]\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "branch",
            "check",
            "--policy",
            str(policy),
            "--name",
            "feat/no-git-needed",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["matched_pattern"] == "feat/*"


def test_option_like_branch_name_reaches_policy_validation(tmp_path: Path) -> None:
    policy = tmp_path / "branch-policy.toml"
    policy.write_text(
        'branch-policy-version = 1\nallowed-patterns = ["feat/*"]\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "branch",
            "check",
            "--policy",
            str(policy),
            "--name=-feat",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1, result.stderr
    document = json.loads(result.stdout)
    assert document["diagnostics"] == [
        {
            "code": "branch.syntax",
            "message": "branch name does not use the portable YAGA branch syntax",
        }
    ]
