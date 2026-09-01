"""Tests for bounded, shell-free Git commit selection."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from yaga.commits import git as commit_git
from yaga.errors import GitError


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
    git(repository, "commit", "--quiet", "--allow-empty", "--file", str(message_file))
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, list[str]]:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.email", "yaga@example.com")
    git(tmp_path, "config", "user.name", "YAGA Tests")
    shas = [
        commit(tmp_path, "feat: add the first command\n\nInitial body."),
        commit(tmp_path, "fix(cli): preserve complete messages"),
        commit(tmp_path, "docs: explain repository ranges"),
    ]
    return tmp_path, shas


def test_read_commit_selects_exactly_one_revision(
    repository: tuple[Path, list[str]],
) -> None:
    path, shas = repository

    selected = commit_git.read_commit(path)

    assert selected.sha == shas[-1]
    assert selected.message.startswith("docs: explain repository ranges")
    assert selected.parents == (shas[-2],)

    previous = commit_git.read_commit(path, "HEAD~1")

    assert previous.sha == shas[-2]


def test_read_range_returns_complete_messages_oldest_first(
    repository: tuple[Path, list[str]],
) -> None:
    path, shas = repository

    selected = commit_git.read_range(path, f"{shas[0]}..HEAD", max_commits=10)

    assert [target.sha for target in selected] == shas[1:]
    assert selected[0].message.startswith("fix(cli):")


def test_git_log_output_is_forced_to_utf8(
    repository: tuple[Path, list[str]],
) -> None:
    path, _ = repository
    git(path, "config", "i18n.logOutputEncoding", "ISO-8859-1")
    sha = commit(path, "feat: support café")

    selected = commit_git.read_commit(path)

    assert selected.sha == sha
    assert selected.message.startswith("feat: support café")


def test_read_range_rejects_empty_and_over_limit_selections(
    repository: tuple[Path, list[str]],
) -> None:
    path, shas = repository

    with pytest.raises(GitError, match="selected no commits"):
        commit_git.read_range(path, "HEAD..HEAD", max_commits=10)
    with pytest.raises(GitError, match="configured limit of 1"):
        commit_git.read_range(path, f"{shas[0]}..HEAD", max_commits=1)


def test_range_rejects_a_shallow_repository(
    repository: tuple[Path, list[str]],
) -> None:
    path, _ = repository
    shallow = path.with_name(f"{path.name}-shallow")
    subprocess.run(
        ["git", "clone", "--depth=2", "--no-local", str(path), str(shallow)],
        check=True,
        capture_output=True,
    )

    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    with pytest.raises(GitError, match="non-shallow"):
        commit_git.read_range(shallow, "HEAD~1..HEAD", max_commits=10)


def test_commit_and_range_sources_reject_ambiguous_revision_shapes(
    repository: tuple[Path, list[str]],
) -> None:
    path, shas = repository

    with pytest.raises(GitError, match="--commit requires exactly one commit-ish"):
        commit_git.read_commit(path, f"{shas[0]}..HEAD")
    with pytest.raises(GitError, match="--commit requires exactly one commit-ish"):
        commit_git.read_commit(path, "HEAD^@")
    with pytest.raises(GitError, match="--commit requires exactly one commit-ish"):
        commit_git.read_commit(path, "HEAD^!")


@pytest.mark.parametrize("revision_range", ["HEAD", "..HEAD", "HEAD..", "HEAD....main"])
def test_range_requires_explicit_base_separator_and_head(
    revision_range: str,
    repository: tuple[Path, list[str]],
) -> None:
    path, _ = repository

    with pytest.raises(GitError, match="--range requires"):
        commit_git.read_range(path, revision_range, max_commits=10)


@pytest.mark.parametrize(
    "revision",
    ["", "--all", "HEAD main", "HEAD\nmain", "HEAD\u202emain"],
)
def test_revision_cannot_be_empty_option_like_or_split(revision: str, tmp_path: Path) -> None:
    with pytest.raises(GitError, match="one bounded"):
        commit_git.read_commit(tmp_path, revision)


def test_git_failures_are_operational_errors_without_control_sequences(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="git could not resolve") as raised:
        commit_git.read_commit(tmp_path, "HEAD")

    assert "\n" not in str(raised.value)


def test_git_error_sanitizer_removes_bidi_and_c1_controls() -> None:
    assert commit_git._safe_git_error("bad\u202e ref\u0085name".encode()) == "bad? ref?name"


@pytest.mark.parametrize("object_id_length", [40, 64])
def test_record_parser_supports_repository_hash_and_retains_parent_identity(
    object_id_length: int,
) -> None:
    sha = b"a" * object_id_length
    parent_one = b"b" * object_id_length
    parent_two = b"c" * object_id_length
    output = b"\0".join([sha, parent_one + b" " + parent_two, b"Merge branch 'main'", b""])

    parsed = commit_git._parse_records(output)

    assert parsed[0].is_merge
    assert parsed[0].sha == sha.decode()
    assert parsed[0].parents == (parent_one.decode(), parent_two.decode())


@pytest.mark.parametrize(
    "sha,parents",
    [
        (b"a" * 39, b""),
        (b"g" * 40, b""),
        (b"a" * 40, b"b" * 39),
        (b"a" * 40, b"b" * 64),
        (b"a" * 64, b"b" * 40),
        (b"a" * 40, b"b" * 40 + b"  " + b"c" * 40),
    ],
)
def test_record_parser_rejects_malformed_commit_and_parent_ids(
    sha: bytes,
    parents: bytes,
) -> None:
    output = b"\0".join([sha, parents, b"fix: bounded identity parsing", b""])

    with pytest.raises(GitError, match="malformed (commit|parent) identity"):
        commit_git._parse_records(output)


@pytest.mark.parametrize(
    ("file_descriptor", "overflow_attribute", "output_attribute"),
    [(1, "stdout_overflow", "stdout"), (2, "stderr_overflow", "stderr")],
)
def test_process_capture_enforces_streaming_pipe_bounds(
    file_descriptor: int,
    overflow_attribute: str,
    output_attribute: str,
) -> None:
    result = commit_git._run_bounded(
        [
            sys.executable,
            "-c",
            f"import os; os.write({file_descriptor}, b'x' * 4096)",
        ],
        stdout_limit=128,
        stderr_limit=128,
    )

    assert getattr(result, overflow_attribute)
    assert len(getattr(result, output_attribute)) == 128


def test_git_log_output_is_stopped_at_the_hard_limit(
    repository: tuple[Path, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    monkeypatch.setattr(commit_git, "MAX_GIT_OUTPUT_BYTES", 32)

    with pytest.raises(GitError, match="hard 32-byte limit"):
        commit_git.read_commit(path)


def test_record_parser_rejects_non_utf8_commit_messages() -> None:
    output = b"a" * 40 + b"\0\0" + b"feat: invalid \xff\0"

    with pytest.raises(GitError, match="not valid UTF-8"):
        commit_git._parse_records(output)
