"""Tests for the isolated dependency-free commit-check Action boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_dependabot_commit_filter import _enable_gpgv, _signed_repository
from tests.test_github_pull_request import (
    DEPENDABOT_AUTHOR,
    _event_document,
    _write_event,
    _write_skip_config,
)
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
    pull_request_title: str = "feat(action): check pull requests",
    config: str | None = None,
    author: dict[str, object] | None = None,
    dependabot_commit: bool = False,
) -> tuple[Path, Path, dict[str, str]]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "YAGA Tests")
    _git(repository, "config", "user.email", "yaga@example.invalid")
    base = _commit(repository, "chore: establish baseline")
    if dependabot_commit:
        _git(repository, "config", "user.name", "dependabot[bot]")
        _git(
            repository, "config", "user.email", "49699333+dependabot[bot]@users.noreply.github.com"
        )
    if config is not None:
        (repository / ".yaga.toml").write_text(config, encoding="utf-8")
        _git(repository, "add", ".yaga.toml")
    head = _commit(repository, head_message)
    if author is None:
        author = {"login": "octocat", "id": 1, "type": "User"}
    event = {
        "action": "opened",
        "number": 17,
        "repository": {"id": 100, "full_name": "owner/repository"},
        "pull_request": {
            "number": 17,
            "title": pull_request_title,
            "state": "open",
            "draft": False,
            "user": author,
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
                "trusted_config": None,
                "trusted_revision": None,
                "trusted_ref": None,
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


@pytest.mark.parametrize("required_version", [">=0.1.0", ">=99.0.0"])
def test_commit_action_runs_without_site_packages_or_installed_cli(
    tmp_path: Path, required_version: str
) -> None:
    repository, event_file, action_environment = _action_fixture(
        tmp_path, config=f'required-version = "{required_version}"\n'
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

    if required_version == ">=99.0.0":
        assert completed.returncode == 2
        assert "does not satisfy" in completed.stderr + completed.stdout
        return
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert "YAGA checked pull request #17" in completed.stdout
    assert "typer" not in completed.stdout.casefold()


@pytest.mark.parametrize("signature_policy", [None, True, False])
@pytest.mark.parametrize("human_addition", [False, True])
def test_isolated_commit_action_requires_signature_unless_explicitly_disabled(
    tmp_path: Path,
    signature_policy: bool | None,
    human_addition: bool,
) -> None:
    signature_config = (
        ""
        if signature_policy is None
        else f"dependabot-require-github-signature = {str(signature_policy).lower()}\n"
    )
    repository, event_file, action_environment = _action_fixture(
        tmp_path,
        head_message="not conventional\n\nDependabot updates are reviewed automatically.",
        pull_request_title="also not conventional",
        config=(
            'config-version = 1\n[commit]\nbody-policy = "required"\n'
            'dependabot-pull-requests = "skip"\n' + signature_config
        ),
        author={"login": "dependabot[bot]", "id": 49_699_333, "type": "Bot"},
        dependabot_commit=True,
    )
    if human_addition:
        _git(repository, "config", "user.name", "YAGA Tests")
        _git(repository, "config", "user.email", "yaga@example.invalid")
        human_head = _commit(repository, "also invalid\n\nA human added this commit.")
        event = json.loads(event_file.read_text(encoding="utf-8"))
        event["pull_request"]["head"]["sha"] = human_head
        event_file.write_text(json.dumps(event), encoding="utf-8")
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

    assert completed.stderr == ""
    if signature_policy is False:
        assert completed.returncode == (1 if human_addition else 0)
        counts = "2 failed, 1 skipped" if human_addition else "0 failed, 2 skipped"
        assert counts in completed.stdout
        assert "Skip reason: Dependabot pull request." in completed.stdout
    else:
        assert completed.returncode == 1
        assert f"{3 if human_addition else 2} failed, 0 skipped" in completed.stdout
        assert "Dependabot pull request" not in completed.stdout


@pytest.mark.parametrize("addition", ["none", "human"])
def test_isolated_commit_action_verifies_real_github_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, addition: str
) -> None:
    _enable_gpgv(monkeypatch)
    repository, base, head = _signed_repository(tmp_path, addition=addition)
    _write_skip_config(repository)
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(base, head, title="invalid edited title", author=DEPENDABOT_AUTHOR),
    )
    environment = dict(
        os.environ,
        GITHUB_BASE_REF="main",
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_HEAD_REF="feature",
        GITHUB_REPOSITORY="owner/repository",
        GITHUB_REPOSITORY_ID="100",
        PYTHONPATH=str(ROOT / "src"),
        YAGA_COMMIT_ACTION_RUNTIME="1",
    )
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
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == (0 if addition == "none" else 1), completed.stdout
    assert completed.stderr == ""
    assert "Skip reason: Dependabot pull request." in completed.stdout
    counts = "0 failed, 2 skipped" if addition == "none" else "2 failed, 1 skipped"
    assert counts in completed.stdout


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
    assert "[body.word-count] line 3, column 1: body has 1 word; minimum is 2" in completed.stdout


@pytest.mark.parametrize(
    "head_message",
    [
        "feat(action)!: require paired markers",
        "feat(action): require paired markers\n\nBREAKING CHANGE: use the new action",
    ],
)
def test_isolated_commit_action_enforces_paired_breaking_markers_only_for_commits(
    tmp_path: Path,
    head_message: str,
) -> None:
    repository, event_file, action_environment = _action_fixture(
        tmp_path,
        head_message=head_message,
        pull_request_title="feat(action)!: describe a breaking pull request",
        config='config-version = 1\n[commit]\nbreaking-markers = "paired"\n',
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

    head_identity = _git(repository, "rev-parse", "--short=12", "HEAD")
    assert completed.returncode == 1
    assert completed.stderr == ""
    assert completed.stdout.count("[breaking.marker-pair]") == 1
    assert f"{head_identity}: [breaking.marker-pair]" in completed.stdout
    assert "pull request #17 title: [breaking.marker-pair]" not in completed.stdout


@pytest.mark.parametrize(
    ("head_message", "diagnostic", "line"),
    [
        (
            "feat(action): require release attribution",
            "footer.required",
            1,
        ),
        (
            "feat(action): reject unfinished commits\n\n"
            "Signed-off-by: YAGA Tests <yaga@example.invalid>\n"
            "WIP: remove before merge",
            "footer.forbidden",
            4,
        ),
    ],
)
def test_isolated_commit_action_enforces_footer_policy_only_for_commits(
    tmp_path: Path,
    head_message: str,
    diagnostic: str,
    line: int,
) -> None:
    repository, event_file, action_environment = _action_fixture(
        tmp_path,
        head_message=head_message,
        pull_request_title="feat(action): describe the attributed pull request",
        config=(
            "config-version = 1\n"
            "[commit]\n"
            'required-footer-tokens = ["Signed-off-by"]\n'
            'forbidden-footer-tokens = ["WIP"]\n'
        ),
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

    head_identity = _git(repository, "rev-parse", "--short=12", "HEAD")
    assert completed.returncode == 1
    assert completed.stderr == ""
    assert completed.stdout.count(f"[{diagnostic}]") == 1
    assert f"{head_identity}: [{diagnostic}] line {line}, column 1:" in completed.stdout
    assert f"pull request #17 title: [{diagnostic}]" not in completed.stdout


def test_isolated_commit_action_applies_per_type_scope_policy_to_pr_title(
    tmp_path: Path,
) -> None:
    repository, event_file, action_environment = _action_fixture(
        tmp_path,
        head_message="feat(action): validate the complete commit",
        pull_request_title="feat: require a title scope",
        config=(
            "config-version = 1\n"
            "[commit]\n"
            'scope-policy = "optional"\n'
            'scope-policy-by-type = { feat = "required" }\n'
        ),
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
    assert completed.stdout.count("[scope.required]") == 1
    assert "pull request #17 title: [scope.required]" in completed.stdout


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
