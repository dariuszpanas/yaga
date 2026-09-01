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


def test_version_is_available_from_the_installed_command() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "yaga 0.1.0"


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


def test_config_show_reports_the_discovered_source(tmp_path: Path) -> None:
    config = tmp_path / ".yaga.toml"
    config.write_text(
        'config-version = 1\n[commit]\nallowed-types = ["feat"]\n',
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
