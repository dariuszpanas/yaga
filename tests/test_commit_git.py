"""Tests for bounded, shell-free Git commit selection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from yaga.commits import git as commit_git
from yaga.commits.models import CommitTarget
from yaga.errors import GitError
from yaga.git import GitRepository
from yaga.git import runtime as git_runtime


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


def test_single_commit_rejects_a_shallow_boundary_that_hides_merge_parents(
    repository: tuple[Path, list[str]],
) -> None:
    path, shas = repository
    tree = git(path, "show", "-s", "--format=%T", shas[-1])
    merge = git(
        path,
        "commit-tree",
        tree,
        "-p",
        shas[-1],
        "-p",
        shas[0],
        "-m",
        "feat: merge shallow boundary work",
    )
    git(path, "update-ref", "refs/heads/main", merge)
    shallow = path.with_name(f"{path.name}-single-shallow")
    subprocess.run(
        ["git", "clone", "--depth=1", "--no-local", str(path), str(shallow)],
        check=True,
        capture_output=True,
    )

    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert git(shallow, "show", "-s", "--format=%P", "HEAD") == ""

    with pytest.raises(GitError, match="non-shallow repository with complete history"):
        commit_git.read_commit(shallow, "HEAD")


def test_single_commit_rejects_graft_rewritten_merge_parents(
    repository: tuple[Path, list[str]],
) -> None:
    path, shas = repository
    tree = git(path, "show", "-s", "--format=%T", shas[-1])
    merge = git(
        path,
        "commit-tree",
        tree,
        "-p",
        shas[-1],
        "-p",
        shas[0],
        "-m",
        "feat: merge independent policy work",
    )
    assert git(path, "show", "-s", "--format=%P", merge) == f"{shas[-1]} {shas[0]}"
    assert commit_git.read_commit(path, merge).parents == (shas[-1], shas[0])

    grafts = path / ".git" / "info" / "grafts"
    grafts.write_text(f"{merge} {shas[-1]}\n", encoding="ascii")
    assert git(path, "show", "-s", "--format=%P", merge) == shas[-1]

    with pytest.raises(GitError, match="legacy graft overlays"):
        commit_git.read_commit(path, merge)


def test_single_commit_revalidates_grafts_after_reading_parent_metadata(
    repository: tuple[Path, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, shas = repository
    grafts = path / ".git" / "info" / "grafts"
    original_read_log = commit_git._read_log

    def read_log_then_add_graft(
        repo: GitRepository,
        selection: str,
        *,
        max_commits: int,
        reverse: bool,
        detect_overflow: bool,
        no_walk: bool,
    ) -> list[CommitTarget]:
        selected = original_read_log(
            repo,
            selection,
            max_commits=max_commits,
            reverse=reverse,
            detect_overflow=detect_overflow,
            no_walk=no_walk,
        )
        grafts.write_text(f"{shas[-1]} {shas[0]}\n", encoding="ascii")
        return selected

    monkeypatch.setattr(commit_git, "_read_log", read_log_then_add_graft)

    with pytest.raises(GitError, match="legacy graft overlays"):
        commit_git.read_commit(path, shas[-1])


def test_range_rejects_legacy_graft_overlays(
    repository: tuple[Path, list[str]],
) -> None:
    path, shas = repository
    grafts = path / ".git" / "info" / "grafts"
    grafts.write_text(f"{shas[-1]} {shas[0]}\n", encoding="ascii")

    with pytest.raises(GitError, match="legacy graft overlays"):
        commit_git.read_range(path, f"{shas[0]}..{shas[-1]}", max_commits=10)


def test_range_revalidates_grafts_after_reading_the_selection(
    repository: tuple[Path, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, shas = repository
    grafts = path / ".git" / "info" / "grafts"
    original_read_log = commit_git._read_log

    def read_log_then_add_graft(
        repo: GitRepository,
        selection: str,
        *,
        max_commits: int,
        reverse: bool,
        detect_overflow: bool,
        no_walk: bool,
    ) -> list[CommitTarget]:
        selected = original_read_log(
            repo,
            selection,
            max_commits=max_commits,
            reverse=reverse,
            detect_overflow=detect_overflow,
            no_walk=no_walk,
        )
        grafts.write_text(f"{shas[-1]} {shas[0]}\n", encoding="ascii")
        return selected

    monkeypatch.setattr(commit_git, "_read_log", read_log_then_add_graft)

    with pytest.raises(GitError, match="legacy graft overlays"):
        commit_git.read_range(path, f"{shas[0]}..{shas[-1]}", max_commits=10)


def test_replacement_refs_cannot_change_selected_commit_message(
    repository: tuple[Path, list[str]],
) -> None:
    path, shas = repository
    original = shas[-1]
    original_tree = git(path, "show", "-s", "--format=%T", original)
    replacement = git(
        path,
        "commit-tree",
        original_tree,
        "-m",
        "fix: replacement message must stay hidden",
    )
    git(path, "replace", original, replacement)
    assert "replacement message" in git(path, "show", "-s", "--format=%B", original)
    assert git(path, "log", "--format=%H", f"{shas[0]}..{original}") == original

    selected = commit_git.read_commit(path, original)
    selected_range = commit_git.read_range(path, f"{shas[0]}..{original}", max_commits=10)

    assert selected.sha == original
    assert selected.parents == (shas[-2],)
    assert selected.message.startswith("docs: explain repository ranges")
    assert [commit.sha for commit in selected_range] == shas[1:]


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


def test_git_executable_inside_enclosing_repository_is_rejected_from_subdirectory(
    repository: tuple[Path, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    subdirectory = path / "src"
    subdirectory.mkdir()
    repository_git = path / "tools" / "git"
    repository_git.parent.mkdir()
    repository_git.write_bytes(b"untrusted")
    monkeypatch.setattr(git_runtime.shutil, "which", lambda _name: str(repository_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        commit_git.read_commit(subdirectory, "HEAD")


def test_git_executable_inside_separate_metadata_directory_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    metadata = tmp_path / "metadata"
    subprocess.run(
        [
            "git",
            "init",
            "--quiet",
            "--initial-branch=main",
            f"--separate-git-dir={metadata}",
            str(worktree),
        ],
        check=True,
        capture_output=True,
    )
    repository_git = metadata / "git.exe"
    repository_git.write_bytes(b"untrusted")
    monkeypatch.setattr(git_runtime.shutil, "which", lambda _name: str(repository_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        commit_git.read_commit(worktree, "HEAD")


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
