"""Tests for the installed explicit committed entry-mode command."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commands import mode as mode_commands
from yaga.errors import GitError
from yaga.modes.checker import check_modes
from yaga.modes.models import ModeEntry, ModeKind, ModePolicy, ModeSelection
from yaga.modes.service import CheckedMode

runner = CliRunner()


def _unstyle(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def _git(repository: Path, *arguments: str, stdin: bytes | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        input=stdin,
    )
    return completed.stdout.decode("ascii").strip()


def _checked(tmp_path: Path, *, failed: bool) -> CheckedMode:
    policy = ModePolicy(1, (ModeKind.REGULAR,))
    entry = ModeEntry(
        "bin/run" if failed else "README.md",
        "c" * 40,
        "100755" if failed else "100644",
        "blob",
    )
    selection = ModeSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        entries=(entry,),
    )
    return CheckedMode(
        report=check_modes(policy, selection),
        policy_path=(tmp_path / "mode-policy.toml").resolve(),
    )


def test_mode_help_exposes_only_explicit_policy_revision_and_runtime_options() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["mode", "--help"])
    command = runner.invoke(app, ["mode", "check", "--help"])

    assert root.exit_code == 0
    assert "mode" in root.stdout
    assert group.exit_code == 0
    assert "Check committed entry-mode policy" in group.stdout
    assert command.exit_code == 0
    help_text = _unstyle(command.stdout)
    for option in ("--policy", "--revision", "--repo", "--format"):
        assert option in help_text
    for output_format in ("text", "json", "github"):
        assert output_format in help_text
    for unsupported in (
        "--current",
        "--event-file",
        "--working-tree",
        "--staged",
        "--content",
        "--token",
        "--quiet",
        "--default-mode",
        "--allow-mode",
        "--pattern",
    ):
        assert unsupported not in help_text


@pytest.mark.parametrize("missing", ["policy", "revision"])
def test_mode_check_requires_both_explicit_inputs(missing: str) -> None:
    arguments = ["mode", "check"]
    if missing != "policy":
        arguments.extend(("--policy", "mode-policy.toml"))
    if missing != "revision":
        arguments.extend(("--revision", "HEAD"))

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert f"--{missing}" in _unstyle(result.stderr)


@pytest.mark.parametrize(("failed", "expected_exit"), [(False, 0), (True, 1)])
def test_mode_check_reports_policy_outcomes_on_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
    expected_exit: int,
) -> None:
    checked = _checked(tmp_path, failed=failed)
    captured: dict[str, object] = {}

    def fake_check_mode_policy(
        repository: Path,
        *,
        policy_path: Path,
        revision: str,
    ) -> CheckedMode:
        captured.update(repository=repository, policy_path=policy_path, revision=revision)
        return checked

    monkeypatch.setattr(mode_commands, "check_mode_policy", fake_check_mode_policy)
    result = runner.invoke(
        app,
        [
            "mode",
            "check",
            "--policy",
            "policy.toml",
            "--revision",
            "release-candidate",
            "--repo",
            str(tmp_path),
            "--format",
            "JSON",
        ],
    )

    assert result.exit_code == expected_exit, result.stderr
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["status"] == ("failed" if failed else "passed")
    assert captured == {
        "repository": tmp_path,
        "policy_path": Path("policy.toml"),
        "revision": "release-candidate",
    }


def test_mode_check_defaults_repository_to_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = _checked(tmp_path, failed=False)
    captured: dict[str, object] = {}

    def fake_check_mode_policy(
        repository: Path,
        *,
        policy_path: Path,
        revision: str,
    ) -> CheckedMode:
        captured.update(repository=repository, policy_path=policy_path, revision=revision)
        return checked

    monkeypatch.setattr(mode_commands, "check_mode_policy", fake_check_mode_policy)
    result = runner.invoke(
        app,
        ["mode", "check", "--policy", "policy.toml", "--revision", "HEAD"],
    )

    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    assert captured["repository"] == Path(".")


@pytest.mark.parametrize("output_format", ["text", "json", "github"])
def test_mode_check_renders_operational_errors_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(
        mode_commands,
        "check_mode_policy",
        lambda *args, **kwargs: (_ for _ in ()).throw(GitError("bad\x1b[31m\nrevision")),
    )

    result = runner.invoke(
        app,
        [
            "mode",
            "check",
            "--policy",
            "policy.toml",
            "--revision",
            "missing",
            "--format",
            output_format,
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "\x1b" not in result.stderr
    if output_format == "json":
        document = json.loads(result.stderr)
        assert document["status"] == "error"
        assert document["error"] == {"kind": "git", "message": "bad?[31m?revision"}
    elif output_format == "github":
        assert result.stderr.strip() == (
            "::error title=YAGA mode policy::YAGA git error: bad?[31m?revision"
        )
    else:
        assert result.stderr.strip() == "YAGA git error: bad?[31m?revision"


def test_mode_check_reads_each_explicit_revision_from_its_exact_committed_tree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "YAGA Tests")
    _git(repository, "config", "user.email", "yaga@example.invalid")
    (repository / "README.md").write_text("committed\n", encoding="utf-8")
    _git(repository, "add", "--", "README.md")
    _git(repository, "commit", "-m", "chore: establish regular mode")
    regular_commit = _git(repository, "rev-parse", "HEAD")
    regular_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    blob = _git(repository, "rev-parse", "HEAD:README.md")

    executable_tree = _git(
        repository,
        "mktree",
        "-z",
        stdin=f"100755 blob {blob}\tbin-run\0".encode(),
    )
    executable_commit = _git(
        repository,
        "commit-tree",
        executable_tree,
        "-p",
        regular_commit,
        stdin=b"chore: select executable mode\n",
    )

    (repository / "README.md").unlink()
    (repository / "untracked.txt").write_text("not committed\n", encoding="utf-8")
    policy = tmp_path / "mode-policy.toml"
    policy.write_text(
        'mode-policy-version = 1\ndefault-allowed-modes = ["regular"]\n',
        encoding="utf-8",
    )

    regular = runner.invoke(
        app,
        [
            "mode",
            "check",
            "--policy",
            str(policy),
            "--revision",
            regular_commit,
            "--repo",
            str(repository),
            "--format",
            "json",
        ],
    )
    executable = runner.invoke(
        app,
        [
            "mode",
            "check",
            "--policy",
            str(policy),
            "--revision",
            executable_commit,
            "--repo",
            str(repository),
            "--format",
            "json",
        ],
    )

    regular_document = json.loads(regular.stdout)
    executable_document = json.loads(executable.stdout)
    assert regular.exit_code == 0, regular.stderr
    assert regular_document["identity"] == {
        "policy_path": str(policy.resolve()),
        "repository_path": str(repository.resolve()),
        "revision": regular_commit,
        "commit_sha": regular_commit,
        "tree_sha": regular_tree,
    }
    assert regular_document["counts"] == {
        "entries": 1,
        "by_mode": {
            "regular": 1,
            "executable": 0,
            "symlink": 0,
            "gitlink": 0,
        },
        "findings": 0,
    }
    assert executable.exit_code == 1, executable.stderr
    assert executable_document["identity"]["commit_sha"] == executable_commit
    assert executable_document["identity"]["tree_sha"] == executable_tree
    assert executable_document["counts"] == {
        "entries": 1,
        "by_mode": {
            "regular": 0,
            "executable": 1,
            "symlink": 0,
            "gitlink": 0,
        },
        "findings": 1,
    }
    assert executable_document["diagnostics"][0] == {
        "code": "mode.disallowed",
        "message": "committed entry mode is not allowed by policy",
        "path": "bin-run",
        "actual_mode": "100755",
        "actual_kind": "executable",
        "allowed_modes": ["regular"],
        "pattern": None,
    }


def test_option_like_revision_reaches_bounded_input_error(tmp_path: Path) -> None:
    policy = tmp_path / "mode-policy.toml"
    policy.write_text(
        'mode-policy-version = 1\ndefault-allowed-modes = ["regular"]\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "mode",
            "check",
            "--policy",
            str(policy),
            "--revision=-bad",
            "--repo",
            str(tmp_path),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"] == {
        "kind": "input",
        "message": (
            "mode revision must be one bounded, non-option commit-ish of at most 512 characters"
        ),
    }
