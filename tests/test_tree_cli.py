"""Tests for the installed explicit committed-tree policy command."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commands import tree as tree_commands
from yaga.errors import GitError
from yaga.trees.models import (
    TREE_REQUIRED_CODE,
    TreeDiagnostic,
    TreePolicy,
    TreeReport,
    TreeSelection,
)
from yaga.trees.service import CheckedTree

runner = CliRunner()


def _unstyle(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _checked(tmp_path: Path, *, failed: bool) -> CheckedTree:
    policy = TreePolicy(
        tree_policy_version=1,
        required_paths=("README.md",),
        forbidden_patterns=("secrets/**",),
    )
    paths = () if failed else ("README.md",)
    selection = TreeSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=paths,
    )
    diagnostics = (
        (
            TreeDiagnostic(
                code=TREE_REQUIRED_CODE,
                message="required tracked path is missing",
                path="README.md",
            ),
        )
        if failed
        else ()
    )
    return CheckedTree(
        report=TreeReport(policy=policy, selection=selection, diagnostics=diagnostics),
        policy_path=(tmp_path / "tree-policy.toml").resolve(),
    )


def test_tree_help_exposes_only_explicit_policy_revision_and_runtime_options() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["tree", "--help"])
    command = runner.invoke(app, ["tree", "check", "--help"])

    assert root.exit_code == 0
    assert "tree" in root.stdout
    assert group.exit_code == 0
    assert "Check committed-tree path policy" in group.stdout
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
    ):
        assert unsupported not in help_text


@pytest.mark.parametrize("missing", ["policy", "revision"])
def test_tree_check_requires_both_explicit_inputs(missing: str) -> None:
    arguments = ["tree", "check"]
    if missing != "policy":
        arguments.extend(("--policy", "tree-policy.toml"))
    if missing != "revision":
        arguments.extend(("--revision", "HEAD"))

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert f"--{missing}" in _unstyle(result.stderr)


@pytest.mark.parametrize(("failed", "expected_exit"), [(False, 0), (True, 1)])
def test_tree_check_reports_policy_outcomes_on_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
    expected_exit: int,
) -> None:
    checked = _checked(tmp_path, failed=failed)
    captured: dict[str, object] = {}

    def fake_check_tree_policy(
        repository: Path,
        *,
        policy_path: Path,
        revision: str,
    ) -> CheckedTree:
        captured.update(
            repository=repository,
            policy_path=policy_path,
            revision=revision,
        )
        return checked

    monkeypatch.setattr(tree_commands, "check_tree_policy", fake_check_tree_policy)
    result = runner.invoke(
        app,
        [
            "tree",
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


def test_tree_check_defaults_repository_to_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = _checked(tmp_path, failed=False)
    captured: dict[str, object] = {}

    def fake_check_tree_policy(
        repository: Path,
        *,
        policy_path: Path,
        revision: str,
    ) -> CheckedTree:
        captured.update(repository=repository, policy_path=policy_path, revision=revision)
        return checked

    monkeypatch.setattr(tree_commands, "check_tree_policy", fake_check_tree_policy)

    result = runner.invoke(
        app,
        [
            "tree",
            "check",
            "--policy",
            "policy.toml",
            "--revision",
            "HEAD",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert captured["repository"] == Path(".")


@pytest.mark.parametrize("output_format", ["text", "json", "github"])
def test_tree_check_renders_operational_errors_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(
        tree_commands,
        "check_tree_policy",
        lambda *args, **kwargs: (_ for _ in ()).throw(GitError("bad\x1b[31m\nrevision")),
    )

    result = runner.invoke(
        app,
        [
            "tree",
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
            "::error title=YAGA tree policy::YAGA git error: bad?[31m?revision"
        )
    else:
        assert result.stderr.strip() == "YAGA git error: bad?[31m?revision"


def test_tree_check_reads_only_the_exact_committed_tree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "YAGA Tests")
    _git(repository, "config", "user.email", "yaga@example.invalid")
    (repository / "README.md").write_text("committed\n", encoding="utf-8")
    _git(repository, "add", "--", "README.md")
    _git(repository, "commit", "-m", "chore: establish tree")
    commit_sha = _git(repository, "rev-parse", "HEAD")
    tree_sha = _git(repository, "rev-parse", "HEAD^{tree}")
    (repository / "README.md").unlink()
    secrets = repository / "secrets"
    secrets.mkdir()
    (secrets / "token.txt").write_text("untracked\n", encoding="utf-8")
    policy = tmp_path / "tree-policy.toml"
    policy.write_text(
        "tree-policy-version = 1\n"
        'required-paths = ["README.md"]\n'
        'forbidden-patterns = ["secrets/**"]\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "tree",
            "check",
            "--policy",
            str(policy),
            "--revision",
            "HEAD",
            "--repo",
            str(repository),
            "--format",
            "json",
        ],
    )
    document = json.loads(result.stdout)

    assert result.exit_code == 0, result.stderr
    assert document["repository_path"] == str(repository.resolve())
    assert document["revision"] == "HEAD"
    assert document["commit_sha"] == commit_sha
    assert document["tree_sha"] == tree_sha
    assert document["entries_checked"] == 1
    assert document["missing_required"] == 0
    assert document["forbidden_paths"] == 0


def test_option_like_revision_reaches_bounded_input_error(tmp_path: Path) -> None:
    policy = tmp_path / "tree-policy.toml"
    policy.write_text(
        'tree-policy-version = 1\nrequired-paths = ["README.md"]\nforbidden-patterns = []\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "tree",
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
            "tree revision must be one bounded, non-option commit-ish of at most 512 characters"
        ),
    }
