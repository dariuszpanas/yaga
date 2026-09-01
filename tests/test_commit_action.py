"""Tests for the isolated dependency-free commit-check Action boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from yaga import commit_action_cli

ROOT = Path(__file__).parents[1]
ACTION_ARGUMENTS = [
    "github",
    "pull-request",
    "check",
    "--event-file",
    "event.json",
    "--repo",
    "repository",
    "--format",
    "github",
]


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _git(repository, "commit", "--allow-empty", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _action_fixture(
    tmp_path: Path,
    *,
    head_message: str = "feat(action): check pull requests",
    config: str | None = None,
) -> tuple[Path, Path, dict[str, str]]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "YAGA Tests")
    _git(repository, "config", "user.email", "yaga@example.invalid")
    base = _commit(repository, "chore: establish baseline")
    if config is not None:
        (repository / ".yaga.toml").write_text(config, encoding="utf-8")
        _git(repository, "add", ".yaga.toml")
    head = _commit(repository, head_message)
    event = {
        "action": "opened",
        "number": 17,
        "repository": {"id": 100, "full_name": "owner/repository"},
        "pull_request": {
            "number": 17,
            "title": "feat(action): check pull requests",
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
    environment = {
        "GITHUB_BASE_REF": "main",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_HEAD_REF": "feature",
        "GITHUB_REPOSITORY": "owner/repository",
        "GITHUB_REPOSITORY_ID": "100",
    }
    return repository, event_file, environment


def test_commit_action_cli_accepts_only_the_fixed_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    for name, value in {
        "GITHUB_BASE_REF": "main",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_HEAD_REF": "feature",
        "GITHUB_REPOSITORY": "owner/repository",
        "GITHUB_REPOSITORY_ID": "100",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        commit_action_cli,
        "run_pull_request_action",
        lambda *args, **kwargs: calls.append((*args, kwargs)) or 0,
    )

    assert commit_action_cli.main(ACTION_ARGUMENTS) == 0
    assert calls == [
        (
            "event.json",
            "repository",
            {
                "event_name": "pull_request",
                "repository": "owner/repository",
                "repository_id": 100,
                "base_ref": "main",
                "head_ref": "feature",
            },
        )
    ]

    for arguments in ([], ["--help"], [*ACTION_ARGUMENTS, "extra"]):
        assert commit_action_cli.main(arguments) == 2


@pytest.mark.parametrize("repository_id", ["", "0", "+1", "01", "9223372036854775808"])
def test_commit_action_cli_rejects_missing_or_invalid_runner_identity(
    repository_id: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_HEAD_REF", "feature")
    if repository_id:
        monkeypatch.setenv("GITHUB_REPOSITORY_ID", repository_id)
    else:
        monkeypatch.delenv("GITHUB_REPOSITORY_ID", raising=False)

    assert commit_action_cli.main(ACTION_ARGUMENTS) == 2
    assert "YAGA failed:" in capsys.readouterr().err


def test_commit_action_runs_without_site_packages_or_installed_cli(tmp_path: Path) -> None:
    repository, event_file, action_environment = _action_fixture(tmp_path)
    environment = os.environ.copy()
    environment.update(action_environment)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["YAGA_COMMIT_ACTION_RUNTIME"] = "1"
    environment.pop("YAGA_ACTION_RUNTIME", None)

    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-S",
            "-m",
            "yaga",
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
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert "YAGA checked pull request #17" in completed.stdout
    assert "typer" not in completed.stdout.casefold()


def test_isolated_commit_action_reports_body_word_policy(tmp_path: Path) -> None:
    repository, event_file, action_environment = _action_fixture(
        tmp_path,
        head_message="feat(action): enforce prose\n\none",
        config="config-version = 1\n[commit]\nbody-min-words = 2\n",
    )
    environment = os.environ.copy()
    environment.update(action_environment)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["YAGA_COMMIT_ACTION_RUNTIME"] = "1"
    environment.pop("YAGA_ACTION_RUNTIME", None)

    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-S",
            "-m",
            "yaga",
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
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert "[body.word-count] line 3: body has 1 word; minimum is 2" in completed.stdout


def test_action_runtime_selectors_are_mutually_exclusive(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["YAGA_ACTION_RUNTIME"] = "1"
    environment["YAGA_COMMIT_ACTION_RUNTIME"] = "1"

    completed = subprocess.run(
        [sys.executable, "-P", "-S", "-m", "yaga"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "YAGA failed: multiple Action runtimes were selected\n"
