"""Tests for the installed explicit committed-path portability command."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commands import path as path_commands
from yaga.errors import GitError
from yaga.paths.checker import check_paths
from yaga.paths.models import PathPolicy, PathSelection
from yaga.paths.service import CheckedPath

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


def _checked(tmp_path: Path, *, failed: bool) -> CheckedPath:
    policy = PathPolicy(
        path_policy_version=1,
        rules=("windows-reserved",),
    )
    selection = PathSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=("CON.txt",) if failed else ("README.md",),
    )
    return CheckedPath(
        report=check_paths(policy, selection),
        policy_path=(tmp_path / "path-policy.toml").resolve(),
    )


def test_path_help_exposes_only_explicit_policy_revision_and_runtime_options() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["path", "--help"])
    command = runner.invoke(app, ["path", "check", "--help"])

    assert root.exit_code == 0
    assert "path" in root.stdout
    assert group.exit_code == 0
    assert "Check committed-path portability policy" in group.stdout
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
        "--profile",
        "--rule",
    ):
        assert unsupported not in help_text


@pytest.mark.parametrize("missing", ["policy", "revision"])
def test_path_check_requires_both_explicit_inputs(missing: str) -> None:
    arguments = ["path", "check"]
    if missing != "policy":
        arguments.extend(("--policy", "path-policy.toml"))
    if missing != "revision":
        arguments.extend(("--revision", "HEAD"))

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert f"--{missing}" in _unstyle(result.stderr)


@pytest.mark.parametrize(("failed", "expected_exit"), [(False, 0), (True, 1)])
def test_path_check_reports_policy_outcomes_on_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
    expected_exit: int,
) -> None:
    checked = _checked(tmp_path, failed=failed)
    captured: dict[str, object] = {}

    def fake_check_path_policy(
        repository: Path,
        *,
        policy_path: Path,
        revision: str,
    ) -> CheckedPath:
        captured.update(repository=repository, policy_path=policy_path, revision=revision)
        return checked

    monkeypatch.setattr(path_commands, "check_path_policy", fake_check_path_policy)
    result = runner.invoke(
        app,
        [
            "path",
            "check",
            "--policy",
            "policy.toml",
            "--revision",
            "release-candidate",
            "--repo",
            str(tmp_path),
            "--format",
            "json",
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


def test_path_check_defaults_repository_to_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = _checked(tmp_path, failed=False)
    captured: dict[str, object] = {}

    def fake_check_path_policy(
        repository: Path,
        *,
        policy_path: Path,
        revision: str,
    ) -> CheckedPath:
        captured.update(repository=repository, policy_path=policy_path, revision=revision)
        return checked

    monkeypatch.setattr(path_commands, "check_path_policy", fake_check_path_policy)
    result = runner.invoke(
        app,
        ["path", "check", "--policy", "policy.toml", "--revision", "HEAD"],
    )

    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    assert captured["repository"] == Path(".")


@pytest.mark.parametrize("output_format", ["text", "json", "github"])
def test_path_check_renders_operational_errors_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(
        path_commands,
        "check_path_policy",
        lambda *args, **kwargs: (_ for _ in ()).throw(GitError("bad\x1b[31m\nrevision")),
    )

    result = runner.invoke(
        app,
        [
            "path",
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
            "::error title=YAGA path policy::YAGA git error: bad?[31m?revision"
        )
    else:
        assert result.stderr.strip() == "YAGA git error: bad?[31m?revision"


def test_path_check_reads_each_explicit_revision_from_its_exact_committed_tree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "YAGA Tests")
    _git(repository, "config", "user.email", "yaga@example.invalid")
    (repository / "README.md").write_text("committed\n", encoding="utf-8")
    _git(repository, "add", "--", "README.md")
    _git(repository, "commit", "-m", "chore: establish portable tree")
    portable_commit = _git(repository, "rev-parse", "HEAD")
    portable_tree = _git(repository, "rev-parse", "HEAD^{tree}")

    blob = _git(repository, "hash-object", "-w", "--stdin", stdin=b"opaque\n")
    incompatible_tree = _git(
        repository,
        "mktree",
        "-z",
        stdin=f"100644 blob {blob}\tCON.txt\0".encode(),
    )
    incompatible_commit = _git(
        repository,
        "commit-tree",
        incompatible_tree,
        "-p",
        portable_commit,
        stdin=b"chore: add incompatible path\n",
    )

    (repository / "README.md").unlink()
    (repository / "untracked.txt").write_text("not committed\n", encoding="utf-8")
    policy = tmp_path / "path-policy.toml"
    policy.write_text(
        'path-policy-version = 1\nprofile = "windows-compatible-v1"\n',
        encoding="utf-8",
    )

    portable = runner.invoke(
        app,
        [
            "path",
            "check",
            "--policy",
            str(policy),
            "--revision",
            portable_commit,
            "--repo",
            str(repository),
            "--format",
            "json",
        ],
    )
    incompatible = runner.invoke(
        app,
        [
            "path",
            "check",
            "--policy",
            str(policy),
            "--revision",
            incompatible_commit,
            "--repo",
            str(repository),
            "--format",
            "json",
        ],
    )

    portable_document = json.loads(portable.stdout)
    incompatible_document = json.loads(incompatible.stdout)
    assert portable.exit_code == 0, portable.stderr
    assert portable_document["identity"] == {
        "policy_path": str(policy.resolve()),
        "repository_path": str(repository.resolve()),
        "revision": portable_commit,
        "commit_sha": portable_commit,
        "tree_sha": portable_tree,
    }
    assert portable_document["counts"] == {
        "paths": 1,
        "components": 1,
        "findings": 0,
        "by_code": {},
    }
    assert incompatible.exit_code == 1, incompatible.stderr
    assert incompatible_document["identity"]["commit_sha"] == incompatible_commit
    assert incompatible_document["identity"]["tree_sha"] == incompatible_tree
    assert incompatible_document["counts"] == {
        "paths": 1,
        "components": 1,
        "findings": 1,
        "by_code": {"path.windows-reserved": 1},
    }
    assert incompatible_document["diagnostics"][0]["path"] == "CON.txt"


def test_option_like_revision_reaches_bounded_input_error(tmp_path: Path) -> None:
    policy = tmp_path / "path-policy.toml"
    policy.write_text(
        'path-policy-version = 1\nrules = ["windows-characters"]\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "path",
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
            "path revision must be one bounded, non-option commit-ish of at most 512 characters"
        ),
    }
