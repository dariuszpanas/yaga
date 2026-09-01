"""Tests for the installed Typer command tree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commands import gate as gate_commands
from yaga.commands import workflow as workflow_commands
from yaga.errors import InputError
from yaga.workflows import lint as workflow_lint
from yaga.workflows.models import (
    WorkflowDiagnostic,
    WorkflowLintReport,
    WorkflowLintResult,
)

runner = CliRunner()


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def commit(repository: Path, message: str) -> str:
    message_file = repository / "message.txt"
    message_file.write_text(message, encoding="utf-8")
    git(repository, "commit", "--allow-empty", "--file", str(message_file))
    return git(repository, "rev-parse", "HEAD")


def test_root_help_exposes_local_policy_and_preserved_gate_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "commit" in result.stdout
    assert "config" in result.stdout
    assert "gate" in result.stdout
    assert "github" in result.stdout
    assert "repo" in result.stdout
    assert "workflow" in result.stdout


def test_version_is_available_from_the_installed_command() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "yaga 0.1.0"


def test_github_pull_request_command_uses_exit_zero_one_and_two(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "YAGA Tests")
    git(repository, "config", "user.email", "yaga@example.invalid")
    base = commit(repository, "chore: establish baseline")
    head = commit(repository, "feat(cli): check a pull request")
    event = {
        "action": "opened",
        "number": 17,
        "repository": {"id": 100, "full_name": "owner/repository"},
        "pull_request": {
            "number": 17,
            "title": "feat(cli): check a pull request",
            "state": "open",
            "draft": False,
            "base": {
                "sha": base,
                "ref": "main",
                "repo": {"id": 100, "full_name": "owner/repository"},
            },
            "head": {
                "sha": head,
                "ref": "feature",
                "repo": {"id": 100, "full_name": "owner/repository"},
            },
        },
    }
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(event), encoding="utf-8")
    arguments = [
        "github",
        "pull-request",
        "check",
        "--event-file",
        str(event_file),
        "--repo",
        str(repository),
        "--format",
        "json",
    ]

    passed = runner.invoke(app, arguments)
    assert passed.exit_code == 0
    assert json.loads(passed.stdout)["kind"] == "pull_request_commit_policy"

    pull_request = event["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["title"] = "not conventional"
    event_file.write_text(json.dumps(event), encoding="utf-8")
    failed = runner.invoke(app, arguments)
    assert failed.exit_code == 1
    assert json.loads(failed.stdout)["valid"] is False

    event_file.write_text("not JSON", encoding="utf-8")
    errored = runner.invoke(app, arguments)
    assert errored.exit_code == 2
    assert json.loads(errored.stderr)["error"]["kind"] == "input"


def test_github_pull_request_cli_reports_body_word_findings_as_annotations(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "YAGA Tests")
    git(repository, "config", "user.email", "yaga@example.invalid")
    base = commit(repository, "chore: establish baseline")
    head = commit(repository, "feat(cli): enforce prose minimum\n\none two")
    (repository / ".yaga.toml").write_text(
        "config-version = 1\n[commit]\nbody-min-words = 3\n",
        encoding="utf-8",
    )
    event = {
        "action": "opened",
        "number": 18,
        "repository": {"id": 100, "full_name": "owner/repository"},
        "pull_request": {
            "number": 18,
            "title": "feat(cli): enforce prose minimum",
            "state": "open",
            "draft": False,
            "base": {
                "sha": base,
                "ref": "main",
                "repo": {"id": 100, "full_name": "owner/repository"},
            },
            "head": {
                "sha": head,
                "ref": "feature",
                "repo": {"id": 100, "full_name": "owner/repository"},
            },
        },
    }
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(event), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "github",
            "pull-request",
            "check",
            "--event-file",
            str(event_file),
            "--repo",
            str(repository),
            "--format",
            "github",
        ],
    )

    assert result.exit_code == 1
    assert "::error title=YAGA commit policy::" in result.stdout
    assert "[body.word-count] line 3: body has 2 words; minimum is 3" in result.stdout


def test_commit_check_accepts_a_message_and_uses_exit_one_for_violations(
    tmp_path: Path,
) -> None:
    valid = runner.invoke(
        app,
        ["commit", "check", "--message", "feat: add a command", "--repo", str(tmp_path)],
    )
    invalid = runner.invoke(
        app,
        ["commit", "check", "--message", "add a command", "--repo", str(tmp_path)],
    )

    assert valid.exit_code == 0
    assert "PASSED" in valid.stdout
    assert invalid.exit_code == 1
    assert "syntax.header" in invalid.stdout


def test_explicit_message_encoding_failure_is_exit_two(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["commit", "check", "--message", "feat: invalid \udcff", "--repo", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "YAGA input error" in result.stderr
    assert "Traceback" not in result.stderr


def test_commit_check_json_is_stable_and_quiet_suppresses_output(tmp_path: Path) -> None:
    structured = runner.invoke(
        app,
        [
            "commit",
            "check",
            "--message",
            "fix: preserve JSON output",
            "--repo",
            str(tmp_path),
            "--format",
            "json",
        ],
    )
    quiet = runner.invoke(
        app,
        [
            "commit",
            "check",
            "--message",
            "not conventional",
            "--repo",
            str(tmp_path),
            "--quiet",
        ],
    )

    assert json.loads(structured.stdout)["valid"] is True
    assert quiet.exit_code == 1
    assert quiet.stdout == ""


def test_message_sources_are_mutually_exclusive_operational_inputs(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "commit",
            "check",
            "--message",
            "feat: add input",
            "--stdin",
            "--repo",
            str(tmp_path),
        ],
        input="fix: stdin\n",
    )

    assert result.exit_code == 2
    assert "choose only one" in result.stderr


def test_file_and_stdin_sources_preserve_full_messages(tmp_path: Path) -> None:
    message_file = tmp_path / "COMMIT_EDITMSG"
    message_file.write_text("feat(cli): read a hook file\n\nUseful body.\n", encoding="utf-8")

    from_file = runner.invoke(
        app,
        ["commit", "check", "--file", str(message_file), "--repo", str(tmp_path)],
    )
    from_stdin = runner.invoke(
        app,
        ["commit", "check", "--stdin", "--repo", str(tmp_path)],
        input="fix(cli): read standard input\n",
    )

    assert from_file.exit_code == 0
    assert from_stdin.exit_code == 0


def test_file_source_preserves_policy_and_operational_exit_codes(tmp_path: Path) -> None:
    message_file = tmp_path / "COMMIT_EDITMSG"
    arguments = [
        "commit",
        "check",
        "--file",
        str(message_file),
        "--repo",
        str(tmp_path),
    ]
    message_file.write_text("not conventional\n", encoding="utf-8")

    failed = runner.invoke(app, arguments)

    assert failed.exit_code == 1
    assert "syntax.header" in failed.stdout

    message_file.write_bytes(b"feat: invalid \xff\n")
    errored = runner.invoke(app, arguments)

    assert errored.exit_code == 2
    assert "YAGA input error" in errored.stderr
    assert "Traceback" not in errored.stderr


def test_default_commit_explicit_commit_and_range_use_the_same_cli_policy(tmp_path: Path) -> None:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.email", "yaga@example.com")
    git(tmp_path, "config", "user.name", "YAGA Tests")
    first = commit(tmp_path, "feat: add the first command")
    commit(tmp_path, "docs: explain the command")
    (tmp_path / ".yaga.toml").write_text(
        'config-version = 1\n[commit]\nallowed-types = ["feat"]\n',
        encoding="utf-8",
    )

    head = runner.invoke(app, ["commit", "check", "--repo", str(tmp_path)])
    selected = runner.invoke(
        app,
        ["commit", "check", "--commit", first, "--repo", str(tmp_path)],
    )
    revision_range = runner.invoke(
        app,
        ["commit", "check", "--range", f"{first}..HEAD", "--repo", str(tmp_path)],
    )

    assert head.exit_code == 1
    assert "type.allowed" in head.stdout
    assert selected.exit_code == 0
    assert revision_range.exit_code == 1
    assert "type.allowed" in revision_range.stdout


def test_git_resolution_failure_is_exit_two(tmp_path: Path) -> None:
    git(tmp_path, "init", "--initial-branch=main")

    result = runner.invoke(
        app,
        ["commit", "check", "--commit", "missing", "--repo", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "YAGA git error" in result.stderr


def test_invalid_utf8_stdin_is_a_sanitized_exit_two(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "yaga",
            "commit",
            "check",
            "--stdin",
            "--repo",
            str(tmp_path),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        input=b"\xff",
        check=False,
        capture_output=True,
    )

    assert result.returncode == 2
    assert result.stdout == b""
    assert b"YAGA input error: standard input is not valid UTF-8" in result.stderr
    assert b"Traceback" not in result.stderr


def test_installed_entrypoint_forces_utf8_output(tmp_path: Path) -> None:
    message_file = tmp_path / "message.txt"
    message_file.write_text("feat: add 🚀 launch support\n", encoding="utf-8")
    executable = Path(sys.executable).with_name("yaga.exe" if os.name == "nt" else "yaga")
    assert executable.is_file()
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp1252"
    environment["PYTHONUTF8"] = "0"

    result = subprocess.run(
        [
            executable,
            "commit",
            "check",
            "--file",
            str(message_file),
            "--repo",
            str(tmp_path),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        check=False,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "feat: add 🚀 launch support" in result.stdout.decode("utf-8")
    assert result.stderr == b""

    missing_file = tmp_path / "missing 🚀 message.txt"
    errored = subprocess.run(
        [
            executable,
            "commit",
            "check",
            "--file",
            str(missing_file),
            "--repo",
            str(tmp_path),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        check=False,
        capture_output=True,
    )
    error_text = errored.stderr.decode("utf-8")

    assert errored.returncode == 2
    assert errored.stdout == b""
    assert str(missing_file.resolve()) in error_text
    assert "Traceback" not in error_text


def test_config_show_reports_the_discovered_source(tmp_path: Path) -> None:
    config = tmp_path / ".yaga.toml"
    config.write_text(
        'config-version = 1\n[commit]\nallowed-types = ["feat"]\nbody-min-words = 4\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["config", "show", "--repo", str(tmp_path), "--format", "json"],
    )
    document = json.loads(result.stdout)

    assert result.exit_code == 0
    assert document["config_path"] == str(config)
    assert document["config"]["allowed_types"] == ["feat"]
    assert document["config"]["body_min_words"] == 4


def test_config_init_creates_and_reports_a_standalone_policy(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["config", "init", "--repo", str(tmp_path), "--format", "json"],
    )
    document = json.loads(result.stdout)
    config = tmp_path / ".yaga.toml"

    assert result.exit_code == 0
    assert result.stderr == ""
    assert config.is_file()
    assert document["schema_version"] == 1
    assert document["config_path"] == str(config)
    assert document["config"]["allowed_types"] == [
        "build",
        "chore",
        "ci",
        "docs",
        "feat",
        "fix",
        "perf",
        "refactor",
        "revert",
        "style",
        "test",
    ]
    assert document["config"]["merge_commits"] == "reject"


def test_config_init_refuses_to_overwrite_and_uses_exit_two(tmp_path: Path) -> None:
    config = tmp_path / ".yaga.toml"
    original = 'config-version = 1\n[commit]\nallowed-types = ["docs"]\n'
    config.write_text(original, encoding="utf-8")

    result = runner.invoke(app, ["config", "init", "--repo", str(tmp_path)])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "already applies" in result.stderr
    assert "Traceback" not in result.stderr
    assert config.read_text(encoding="utf-8") == original


def test_config_help_explains_safe_standalone_initialization() -> None:
    result = runner.invoke(app, ["config", "init", "--help"])

    assert result.exit_code == 0
    assert "standalone .yaga.toml" in result.stdout
    assert "without overwriting" in result.stdout


def test_workflow_check_uses_exit_zero_one_and_two(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    prefix = "name: CI\non: push\njobs:\n  check:\n    runs-on: ubuntu-latest\n    steps:\n"
    workflow.write_text(
        prefix + "      - uses: actions/checkout@" + "a" * 40 + "\n",
        encoding="utf-8",
    )
    arguments = [
        "workflow",
        "check",
        "--repo",
        str(tmp_path),
        "--format",
        "json",
    ]

    passed = runner.invoke(app, arguments)
    assert passed.exit_code == 0
    assert json.loads(passed.stdout)["kind"] == "github_workflow_policy"

    workflow.write_text(prefix + "      - uses: actions/checkout@main\n", encoding="utf-8")
    failed = runner.invoke(app, arguments)
    assert failed.exit_code == 1
    assert json.loads(failed.stdout)["workflows"][0]["diagnostics"][0]["code"] == "uses.pin"

    workflow.write_text("jobs: [\n", encoding="utf-8")
    errored = runner.invoke(app, arguments)
    assert errored.exit_code == 2
    assert json.loads(errored.stderr)["error"]["kind"] == "input"


def test_workflow_help_describes_default_and_explicit_paths() -> None:
    result = runner.invoke(app, ["workflow", "check", "--help"])

    assert result.exit_code == 0
    assert ".github/workflows" in result.stdout
    assert "immutable external references" in result.stdout


def test_workflow_lint_uses_exit_zero_one_and_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = [
        "workflow",
        "lint",
        "examples",
        "--repo",
        str(tmp_path),
        "--format",
        "json",
    ]

    monkeypatch.setattr(
        workflow_commands,
        "lint_workflows",
        lambda _repository, _selections: WorkflowLintReport(
            (WorkflowLintResult(path="examples/valid.yml", diagnostics=()),)
        ),
    )
    passed = runner.invoke(app, arguments)
    assert passed.exit_code == 0
    assert json.loads(passed.stdout)["kind"] == "github_workflow_lint"

    monkeypatch.setattr(
        workflow_commands,
        "lint_workflows",
        lambda _repository, _selections: WorkflowLintReport(
            (
                WorkflowLintResult(
                    path="examples/invalid.yml",
                    diagnostics=(
                        WorkflowDiagnostic(
                            code="syntax-check",
                            message="workflow is invalid",
                            line=2,
                            column=4,
                        ),
                    ),
                ),
            )
        ),
    )
    failed = runner.invoke(app, arguments)
    assert failed.exit_code == 1
    assert json.loads(failed.stdout)["diagnostics"] == 1

    def fail_lint(_repository: Path, _selections: object) -> WorkflowLintReport:
        raise InputError("Docker is unavailable")

    monkeypatch.setattr(workflow_commands, "lint_workflows", fail_lint)
    errored = runner.invoke(app, arguments)
    assert errored.exit_code == 2
    assert json.loads(errored.stderr)["error"]["kind"] == "input"


def test_workflow_lint_help_exposes_pinned_runtime_and_paths() -> None:
    group = runner.invoke(app, ["workflow", "--help"])
    lint = runner.invoke(app, ["workflow", "lint", "--help"])

    assert group.exit_code == 0
    assert "lint" in group.stdout
    assert "Inspect GitHub workflows" in group.stdout
    assert "workflow policy" not in group.stdout
    assert lint.exit_code == 0
    assert ".github/workflows" in lint.stdout
    assert "pinned actionlint container" in lint.stdout


def test_workflow_lint_temporary_workspace_failures_are_bounded_json_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("jobs: {}\n", encoding="utf-8")
    monkeypatch.setattr(workflow_lint, "_find_docker", lambda repository: "C:/tools/docker.exe")

    def fail_temporary_directory(*args: object, **kwargs: object) -> object:
        raise OSError("attacker-controlled temporary failure")

    monkeypatch.setattr(
        workflow_lint.tempfile,
        "TemporaryDirectory",
        fail_temporary_directory,
    )

    result = runner.invoke(
        app,
        ["workflow", "lint", "--repo", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 2
    document = json.loads(result.stderr)
    assert document["error"] == {
        "kind": "input",
        "message": "actionlint temporary workspace could not be managed safely",
    }


def test_gate_help_lists_every_preserved_operation() -> None:
    result = runner.invoke(app, ["gate", "codex-review", "--help"])

    assert result.exit_code == 0
    for operation in ("authorize", "finalize", "invalidate", "observe", "prepare", "request"):
        assert operation in result.stdout


@pytest.mark.parametrize(
    "operation",
    ["authorize", "finalize", "invalidate", "observe", "prepare", "request"],
)
def test_installed_gate_commands_share_the_action_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gate_commands,
        "run_gate",
        lambda gate, selected: calls.append((gate, selected)) or 0,
    )

    result = runner.invoke(app, ["gate", "codex-review", operation])

    assert result.exit_code == 0
    assert calls == [("codex-review", operation)]
