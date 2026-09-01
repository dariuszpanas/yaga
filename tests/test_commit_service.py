"""Tests for shared dependency-light commit validation services."""

from __future__ import annotations

import io
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from yaga.commits import check_commits, check_git_commits
from yaga.commits.models import ValidationReport
from yaga.errors import InputError


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def commit(repository: Path, message: str) -> str:
    git(repository, "commit", "--quiet", "--allow-empty", "--message", message)
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, list[str], Path]:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.email", "yaga@example.com")
    git(tmp_path, "config", "user.name", "YAGA Tests")
    config = tmp_path / ".yaga.toml"
    config.write_text(
        'config-version = 1\n\n[commit]\nallowed-types = ["service"]\n',
        encoding="utf-8",
    )
    shas = [
        commit(tmp_path, "service: add the first service target"),
        commit(tmp_path, "service: add the second service target"),
        commit(tmp_path, "service: add the third service target"),
    ]
    return tmp_path, shas, config


def test_standalone_service_preserves_message_file_and_stdin_sources(
    repository: tuple[Path, list[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, config = repository

    message_report = check_commits(repo, message="service: check an explicit message")
    assert message_report.valid
    assert message_report.config_path == config
    assert message_report.results[0].target.label == "message"

    message_file = tmp_path / "COMMIT_EDITMSG"
    message_file.write_bytes(b"service: check a complete file\n\nBody.\n")
    file_report = check_commits(repo, file=message_file)
    assert file_report.valid
    assert file_report.results[0].target.message.endswith("Body.\n")
    assert file_report.results[0].target.label == str(message_file.resolve())

    monkeypatch.setattr(sys, "stdin", io.StringIO("service: check standard input\n\nBody.\n"))
    stdin_report = check_commits(repo, stdin=True)
    assert stdin_report.valid
    assert stdin_report.results[0].target.label == "stdin"
    assert stdin_report.results[0].target.message.endswith("Body.\n")

    rejected = check_commits(repo, message="feat: use the discovered policy")
    assert rejected.valid is False
    assert rejected.results[0].diagnostics[0].code == "type.allowed"


def test_standalone_service_preserves_default_commit_explicit_commit_and_range(
    repository: tuple[Path, list[str], Path],
) -> None:
    repo, shas, config = repository

    default_report = check_commits(repo)
    explicit_report = check_commits(repo, commit="HEAD~1")
    range_report = check_commits(repo, revision_range=f"{shas[0]}..HEAD")

    assert default_report.config_path == config
    assert default_report.results[0].target.sha == shas[2]
    assert explicit_report.results[0].target.sha == shas[1]
    assert [result.target.sha for result in range_report.results] == shas[1:]
    assert all(report.valid for report in (default_report, explicit_report, range_report))


def test_standalone_service_applies_footer_policy_to_messages_and_git_commits(
    repository: tuple[Path, list[str], Path],
) -> None:
    repo, _, config = repository
    config.write_text(
        "config-version = 1\n"
        "\n"
        "[commit]\n"
        'allowed-types = ["service"]\n'
        'required-footer-tokens = ["Signed-off-by"]\n'
        'forbidden-footer-tokens = ["WIP"]\n',
        encoding="utf-8",
    )

    missing = check_commits(repo, message="service: require release attribution")
    assert [diagnostic.code for diagnostic in missing.results[0].diagnostics] == ["footer.required"]

    forbidden_sha = commit(
        repo,
        "service: reject unfinished commits\n\n"
        "Signed-off-by: YAGA Tests <yaga@example.com>\n"
        "WIP #remove before merge",
    )
    forbidden = check_commits(repo, commit=forbidden_sha)
    assert [diagnostic.code for diagnostic in forbidden.results[0].diagnostics] == [
        "footer.forbidden"
    ]
    assert forbidden.results[0].diagnostics[0].line == 4

    repeated = check_commits(
        repo,
        message=(
            "service: accept repeated release attribution\n\n"
            "Signed-off-by: First Author <first@example.com>\n"
            "Signed-off-by: Second Author <second@example.com>"
        ),
    )
    assert repeated.valid


def test_git_only_service_exposes_default_commit_explicit_commit_and_range(
    repository: tuple[Path, list[str], Path],
) -> None:
    repo, shas, config = repository

    default_report = check_git_commits(repo)
    explicit_report = check_git_commits(repo, commit="HEAD~1")
    range_report = check_git_commits(repo, revision_range=f"{shas[0]}..HEAD")

    assert default_report.config_path == config
    assert default_report.results[0].target.sha == shas[2]
    assert explicit_report.results[0].target.sha == shas[1]
    assert [result.target.sha for result in range_report.results] == shas[1:]


@pytest.mark.parametrize(
    ("message", "file", "stdin", "commit_revision", "revision_range"),
    [
        ("service: one", Path("message.txt"), False, None, None),
        (None, None, True, "HEAD", None),
        (None, None, False, "HEAD", "HEAD~1..HEAD"),
    ],
)
def test_standalone_service_rejects_multiple_sources_before_repository_work(
    message: str | None,
    file: Path | None,
    stdin: bool,
    commit_revision: str | None,
    revision_range: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yaga.commits.service._resolve_repository",
        lambda _repository: pytest.fail("repository resolution must not run"),
    )

    with pytest.raises(
        InputError,
        match=r"choose only one of --message, --file, --stdin, --commit, or --range",
    ):
        check_commits(
            Path("missing"),
            message=message,
            file=file,
            stdin=stdin,
            commit=commit_revision,
            revision_range=revision_range,
        )


def test_git_only_service_rejects_commit_and_range_before_repository_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yaga.commits.service._resolve_repository",
        lambda _repository: pytest.fail("repository resolution must not run"),
    )

    with pytest.raises(InputError, match=r"choose only one of --commit or --range"):
        check_git_commits(Path("missing"), commit="HEAD", revision_range="HEAD~1..HEAD")


@pytest.mark.parametrize("service", [check_commits, check_git_commits])
def test_repository_resolution_errors_are_bounded(
    service: Callable[[Path], ValidationReport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_expanduser(_path: Path) -> Path:
        raise OSError("attacker-controlled path detail")

    monkeypatch.setattr(Path, "expanduser", fail_expanduser)

    with pytest.raises(InputError, match="commit repository path cannot be resolved") as raised:
        service(Path("repository"))

    assert "attacker-controlled" not in str(raised.value)


def test_explicit_config_is_preserved_for_non_git_sources(tmp_path: Path) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text(
        '[tool.yaga]\nconfig-version = 1\n\n[tool.yaga.commit]\nallowed-types = ["custom"]\n',
        encoding="utf-8",
    )

    report = check_commits(
        tmp_path / "repository-need-not-exist",
        message="custom: use explicit configuration",
        config=config,
    )

    assert report.valid
    assert report.config_path == config.resolve()
