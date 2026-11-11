"""Tests for the unified root GitHub Action's CLI check dispatcher and shell guards."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from yaga import cli_action
from yaga.errors import InputError

ROOT = Path(__file__).resolve().parents[1]


def _base_env(workspace: Path, **overrides: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_WORKSPACE": str(workspace),
        "YAGA_NON_CLI_INPUTS": "0",
        "YAGA_HAS_GITHUB_TOKEN": "0",
        "YAGA_GH_TOKEN": "",
        "GITHUB_TOKEN": "ambient-should-be-stripped",
        "GH_TOKEN": "ambient-gh-should-be-stripped",
        "PYTHONPATH": "ambient-pythonpath-should-be-stripped",
    }
    env.update(overrides)
    return env


def test_pr_review_live_mode_builds_exact_command_and_isolates_gh_token(tmp_path: Path) -> None:
    args, child_env, workspace = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK="pr-review",
            YAGA_INPUT_POLICY=".yaga/agent-review-policy.toml",
            YAGA_INPUT_PR="42",
            YAGA_INPUT_REPO="owner/repo",
            YAGA_INPUT_WAIT_SECONDS="300",
            YAGA_INPUT_SUBMIT_REVIEW="both",
            YAGA_INPUT_TRIAGE_POLICY=".yaga/triage-policy.toml",
            YAGA_HAS_GITHUB_TOKEN="1",
            YAGA_GH_TOKEN="ghs_live_review_token",
        )
    )

    assert workspace == tmp_path
    assert args == [
        "pull-request",
        "review",
        "check",
        "--policy=.yaga/agent-review-policy.toml",
        "--pr=42",
        "--repo=owner/repo",
        "--wait-seconds=300",
        "--submit-review=both",
        "--triage-policy=.yaga/triage-policy.toml",
        "--format=github",
    ]
    assert child_env["GH_TOKEN"] == "ghs_live_review_token"
    assert "GITHUB_TOKEN" not in child_env
    assert "PYTHONPATH" not in child_env
    assert not any(key.startswith("YAGA_") for key in child_env)


def test_pr_review_file_mode_rejects_live_inputs_and_strips_all_tokens(tmp_path: Path) -> None:
    args, child_env, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK="pr-review",
            YAGA_INPUT_POLICY="policy.toml",
            YAGA_INPUT_FILE="snapshot.json",
        )
    )
    assert args == [
        "pull-request",
        "review",
        "check",
        "--policy=policy.toml",
        "--file=snapshot.json",
        "--format=github",
    ]
    assert "GH_TOKEN" not in child_env
    assert "GITHUB_TOKEN" not in child_env

    for forbidden_key, value in (
        ("YAGA_INPUT_REPO", "owner/repo"),
        ("YAGA_INPUT_WAIT_SECONDS", "60"),
        ("YAGA_INPUT_SUBMIT_REVIEW", "approve"),
        ("YAGA_INPUT_TRIAGE_POLICY", "trusted-triage.toml"),
        ("YAGA_HAS_GITHUB_TOKEN", "1"),
    ):
        with pytest.raises(InputError):
            cli_action.build_cli_invocation(
                _base_env(
                    tmp_path,
                    YAGA_INPUT_CHECK="pr-review",
                    YAGA_INPUT_POLICY="policy.toml",
                    YAGA_INPUT_FILE="snapshot.json",
                    **{forbidden_key: value},
                )
            )


@pytest.mark.parametrize("publish", ["", "false", "true"])
def test_pr_triage_action_live_command_and_token_scope(tmp_path: Path, publish: str) -> None:
    args, child_env, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK="pr-triage",
            YAGA_INPUT_POLICY="trusted.toml",
            YAGA_INPUT_PR="23",
            YAGA_INPUT_REPO="example/project",
            YAGA_INPUT_PUBLISH_COMMENT=publish,
            YAGA_GH_TOKEN="fake-test-token",
            YAGA_HAS_GITHUB_TOKEN="1",
        )
    )
    assert args == [
        "pull-request",
        "triage",
        "check",
        "--policy=trusted.toml",
        "--pr=23",
        "--repo=example/project",
        *(["--publish-comment"] if publish == "true" else []),
        "--format=github",
    ]
    assert child_env["GH_TOKEN"] == "fake-test-token"
    assert "GITHUB_TOKEN" not in child_env


@pytest.mark.parametrize("publish", ["True", "1", "yes", "--help"])
def test_pr_triage_action_rejects_nonboolean_publication_input(
    tmp_path: Path, publish: str
) -> None:
    with pytest.raises(InputError, match="true or false"):
        cli_action.build_cli_invocation(
            _base_env(
                tmp_path,
                YAGA_INPUT_CHECK="pr-triage",
                YAGA_INPUT_POLICY="trusted.toml",
                YAGA_INPUT_PR="23",
                YAGA_INPUT_REPO="example/project",
                YAGA_INPUT_PUBLISH_COMMENT=publish,
                YAGA_GH_TOKEN="fake-test-token",
            )
        )


def test_pr_triage_action_offline_is_tokenless_and_cannot_publish(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        YAGA_INPUT_CHECK="pr-triage",
        YAGA_INPUT_POLICY="trusted.toml",
        YAGA_INPUT_FILE="snapshot.json",
    )
    args, child_env, _ = cli_action.build_cli_invocation(env)
    assert args == [
        "pull-request",
        "triage",
        "check",
        "--policy=trusted.toml",
        "--file=snapshot.json",
        "--format=github",
    ]
    assert "GH_TOKEN" not in child_env and "GITHUB_TOKEN" not in child_env
    for addition in (
        {"YAGA_INPUT_PUBLISH_COMMENT": "true"},
        {"YAGA_INPUT_REPO": "example/project"},
        {"YAGA_GH_TOKEN": "fake-test-token"},
        {"YAGA_INPUT_SUBMIT_REVIEW": "approve"},
    ):
        with pytest.raises(InputError):
            cli_action.build_cli_invocation(dict(env, **addition))


def test_pr_description_event_file_and_live_modes(tmp_path: Path) -> None:
    event_args, event_env, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            GITHUB_EVENT_PATH="/tmp/event.json",
            YAGA_INPUT_CHECK="pr-description",
            YAGA_INPUT_POLICY=".yaga/description-policy.toml",
            YAGA_INPUT_TEMPLATE=".github/PULL_REQUEST_TEMPLATE.md",
        )
    )
    assert event_args == [
        "github",
        "pull-request",
        "description",
        "check",
        "--policy=.yaga/description-policy.toml",
        "--event-file=/tmp/event.json",
        "--template=.github/PULL_REQUEST_TEMPLATE.md",
        "--format=github",
    ]
    assert "GH_TOKEN" not in event_env

    file_args, file_env, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK="pr-description",
            YAGA_INPUT_POLICY=".yaga/description-policy.toml",
            YAGA_INPUT_FILE="pr-body.md",
        )
    )
    assert file_args == [
        "pull-request",
        "description",
        "check",
        "--policy=.yaga/description-policy.toml",
        "--file=pr-body.md",
        "--format=github",
    ]
    assert "GH_TOKEN" not in file_env

    live_args, live_env, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK="pr-description",
            YAGA_INPUT_POLICY=".yaga/description-policy.toml",
            YAGA_INPUT_PR="19",
            YAGA_INPUT_REPO="owner/repo",
            YAGA_HAS_GITHUB_TOKEN="1",
            YAGA_GH_TOKEN="ghs_desc_token",
        )
    )
    assert live_args == [
        "pull-request",
        "description",
        "check",
        "--policy=.yaga/description-policy.toml",
        "--pr=19",
        "--repo=owner/repo",
        "--format=github",
    ]
    assert live_env["GH_TOKEN"] == "ghs_desc_token"


@pytest.mark.parametrize("check", ["mode", "path", "size", "tree"])
def test_committed_tree_checks_build_exact_commands(tmp_path: Path, check: str) -> None:
    args, child_env, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK=check,
            YAGA_INPUT_POLICY=f".yaga/{check}-policy.toml",
            YAGA_INPUT_REVISION="deadbeef",
            YAGA_INPUT_REPO="subrepo",
        )
    )
    assert args == [
        check,
        "check",
        f"--policy=.yaga/{check}-policy.toml",
        "--revision=deadbeef",
        "--repo=subrepo",
        "--format=github",
    ]
    assert "GH_TOKEN" not in child_env


def test_branch_change_workflow_and_repo_modes_build_exact_commands(tmp_path: Path) -> None:
    branch_args, _, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK="branch",
            YAGA_INPUT_POLICY=".yaga/branch-policy.toml",
            YAGA_INPUT_BRANCH="feat/unified-action",
        )
    )
    assert branch_args == [
        "branch",
        "check",
        "--policy=.yaga/branch-policy.toml",
        "--name=feat/unified-action",
        "--format=github",
    ]

    change_args, _, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK="change",
            YAGA_INPUT_POLICY=".yaga/change-policy.toml",
            YAGA_INPUT_RANGE="abc123...def456",
        )
    )
    assert change_args == [
        "change",
        "check",
        "--policy=.yaga/change-policy.toml",
        "--range=abc123...def456",
        "--format=github",
    ]

    wf_sec_args, _, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK="workflow-security",
            YAGA_INPUT_WORKFLOW_PATHS=".github/workflows\nexamples/with space.yml\n",
            YAGA_INPUT_WORKFLOW_SECURITY_PROFILE="recommended-v3",
            YAGA_INPUT_WORKFLOW_SECURITY_RULES="permissions.explicit, secrets.inherit",
        )
    )
    assert wf_sec_args == [
        "workflow",
        "security",
        "--profile=recommended-v3",
        "--rule=permissions.explicit",
        "--rule=secrets.inherit",
        "--format=github",
        "--",
        ".github/workflows",
        "examples/with space.yml",
    ]

    wf_lint_args, _, _ = cli_action.build_cli_invocation(
        _base_env(tmp_path, YAGA_INPUT_CHECK="workflow-lint")
    )
    assert wf_lint_args == ["workflow", "lint", "--format=github"]

    policy_default_args, policy_env, _ = cli_action.build_cli_invocation(
        _base_env(tmp_path, YAGA_INPUT_CHECK="policy")
    )
    assert policy_default_args == ["policy", "check", "--strict", "--format=github"]
    assert "GH_TOKEN" not in policy_env

    policy_explicit_args, _, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK="policy",
            YAGA_INPUT_REPO="subdir",
            YAGA_INPUT_POLICY=".yaga/branch-policy.toml",
        )
    )
    assert policy_explicit_args == [
        "policy",
        "check",
        "--repo=subdir",
        "--strict",
        "--format=github",
        "--",
        ".yaga/branch-policy.toml",
    ]

    repo_plan_args, _, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK="repo",
            YAGA_INPUT_PLAN=".yaga/checks/ci.toml",
            YAGA_INPUT_COMMIT="HEAD",
            YAGA_INPUT_REVISION="HEAD",
        )
    )
    assert repo_plan_args == [
        "repo",
        "check",
        "--plan=.yaga/checks/ci.toml",
        "--commit=HEAD",
        "--revision=HEAD",
        "--format=github",
    ]

    repo_explicit_args, _, _ = cli_action.build_cli_invocation(
        _base_env(
            tmp_path,
            YAGA_INPUT_CHECK="repo",
            YAGA_INPUT_CHECKS="commit, workflow\ntree",
            YAGA_INPUT_RANGE="a..b",
            YAGA_INPUT_REVISION="b",
            YAGA_INPUT_WORKFLOW_PATHS=".github/workflows",
            YAGA_INPUT_TREE_POLICY=".yaga/tree-policy.toml",
        )
    )
    assert repo_explicit_args == [
        "repo",
        "check",
        "--check=commit",
        "--check=workflow",
        "--check=tree",
        "--range=a..b",
        "--revision=b",
        "--workflow-path=.github/workflows",
        "--tree-policy=.yaga/tree-policy.toml",
        "--format=github",
    ]


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"YAGA_INPUT_CHECK": "commit"}, "check mode is invalid"),
        ({"YAGA_INPUT_CHECK": "gate"}, "check mode is invalid"),
        ({"YAGA_INPUT_CHECK": "unknown"}, "check mode is invalid"),
        (
            {
                "YAGA_INPUT_CHECK": "branch",
                "YAGA_INPUT_POLICY": "p.toml",
                "YAGA_INPUT_BRANCH": "feat/x",
                "YAGA_NON_CLI_INPUTS": "1",
            },
            "commit or gate inputs",
        ),
        (
            {
                "YAGA_INPUT_CHECK": "branch",
                "YAGA_INPUT_POLICY": "p.toml",
                "YAGA_INPUT_BRANCH": "feat/x",
                "YAGA_INPUT_REVISION": "HEAD",
            },
            "does not accept input 'revision'",
        ),
        (
            {
                "YAGA_INPUT_CHECK": "branch",
                "YAGA_INPUT_POLICY": "p.toml",
                "YAGA_INPUT_BRANCH": "feat/x",
                "YAGA_HAS_GITHUB_TOKEN": "1",
            },
            "does not accept input 'github-token'",
        ),
        (
            {
                "YAGA_INPUT_CHECK": "pr-review",
                "YAGA_INPUT_POLICY": "p.toml",
                "YAGA_INPUT_PR": "12",
            },
            "requires input 'github-token'",
        ),
        (
            {
                "YAGA_INPUT_CHECK": "workflow",
                "YAGA_INPUT_WORKFLOW_PATHS": "--help",
            },
            "must not start with '-'",
        ),
        (
            {
                "YAGA_INPUT_CHECK": "repo",
                "YAGA_INPUT_CHECKS": "commit,,tree",
            },
            "empty list item",
        ),
        (
            {
                "YAGA_INPUT_CHECK": "repo",
                "YAGA_INPUT_CHECKS": "--help",
            },
            "must not start with '-'",
        ),
        (
            {
                "YAGA_INPUT_CHECK": "branch",
                "YAGA_INPUT_POLICY": "bad\x00.toml",
                "YAGA_INPUT_BRANCH": "feat/x",
            },
            "NUL byte",
        ),
    ],
)
def test_cli_action_rejects_invalid_or_cross_mode_inputs(
    tmp_path: Path,
    overrides: dict[str, str],
    match: str,
) -> None:
    with pytest.raises(InputError, match=match):
        cli_action.build_cli_invocation(_base_env(tmp_path, **overrides))


def test_cli_action_main_executes_real_yaga_checks_and_blocks_help_injection(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "branch-policy.toml"
    policy.write_text(
        'branch-policy-version = 1\nallowed-patterns = ["feat/*"]\n',
        encoding="utf-8",
    )

    env = dict(
        os.environ,
        GITHUB_WORKSPACE=str(tmp_path),
        YAGA_INPUT_CHECK="branch",
        YAGA_INPUT_POLICY=str(policy),
        YAGA_INPUT_BRANCH="feat/clean",
        YAGA_NON_CLI_INPUTS="0",
        YAGA_HAS_GITHUB_TOKEN="0",
    )
    passed = subprocess.run(
        [sys.executable, "-m", "yaga.cli_action"],
        cwd=ROOT,
        env={**env, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert "passed" in passed.stdout

    help_attempt = subprocess.run(
        [sys.executable, "-m", "yaga.cli_action"],
        cwd=ROOT,
        env={**env, "YAGA_INPUT_BRANCH": "--help", "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert help_attempt.returncode == 1
    assert "branch.syntax" in help_attempt.stdout
    assert "Usage:" not in help_attempt.stdout

    desc_policy = tmp_path / "description-policy.toml"
    desc_policy.write_text("description-policy-version = 1\n", encoding="utf-8")
    event_file = tmp_path / "event.json"
    event_file.write_text(
        json.dumps(
            {
                "action": "opened",
                "number": 7,
                "repository": {"id": 1, "full_name": "owner/repo"},
                "pull_request": {
                    "number": 7,
                    "state": "open",
                    "draft": False,
                    "body": "Explain why the unified Action keeps runtimes isolated.\n",
                },
            }
        ),
        encoding="utf-8",
    )
    desc_passed = subprocess.run(
        [sys.executable, "-m", "yaga.cli_action"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "GITHUB_WORKSPACE": str(tmp_path),
            "GITHUB_EVENT_PATH": str(event_file),
            "YAGA_INPUT_CHECK": "pr-description",
            "YAGA_INPUT_POLICY": str(desc_policy),
            "YAGA_NON_CLI_INPUTS": "0",
            "YAGA_HAS_GITHUB_TOKEN": "0",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert desc_passed.returncode == 0, desc_passed.stdout + desc_passed.stderr


def test_action_manifest_shell_guards_reject_invalid_selector_and_unused_inputs(
    tmp_path: Path,
) -> None:
    bash = "C:/Program Files/Git/bin/bash.exe" if sys.platform == "win32" else shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash is unavailable")

    manifest = yaml.safe_load((ROOT / "action.yml").read_text("utf-8"))
    steps_by_id = {step["id"]: step["run"] for step in manifest["runs"]["steps"] if "id" in step}

    invalid_out = tmp_path / "invalid_out"
    invalid_summary = tmp_path / "invalid_summary"
    invalid_res = subprocess.run(
        [bash, "-c", steps_by_id["invalid_check"]],
        env={
            **os.environ,
            "GITHUB_OUTPUT": invalid_out.as_posix(),
            "GITHUB_STEP_SUMMARY": invalid_summary.as_posix(),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert invalid_res.returncode == 2
    assert "exit-code=2" in invalid_out.read_text("utf-8")
    assert "unsupported check mode" in invalid_res.stderr
    assert "unsupported check mode" in invalid_summary.read_text("utf-8")

    commit_out = tmp_path / "commit_out"
    commit_summary = tmp_path / "commit_summary"
    commit_res = subprocess.run(
        [bash, "-c", steps_by_id["commit"]],
        env={
            **os.environ,
            "GITHUB_ACTION_PATH": ROOT.as_posix(),
            "GITHUB_OUTPUT": commit_out.as_posix(),
            "GITHUB_STEP_SUMMARY": commit_summary.as_posix(),
            "YAGA_UNUSED_INPUTS": "1",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert commit_res.returncode == 2
    assert "exit-code=2" in commit_out.read_text("utf-8")
    assert "accepts only 'check' and 'trusted-config'" in commit_res.stderr
    assert "accepts only 'check' and 'trusted-config'" in commit_summary.read_text("utf-8")


def test_pytest_isolates_live_github_runner_command_files() -> None:
    for name in (
        "GITHUB_ENV",
        "GITHUB_OUTPUT",
        "GITHUB_PATH",
        "GITHUB_STATE",
        "GITHUB_STEP_SUMMARY",
    ):
        assert name not in os.environ
