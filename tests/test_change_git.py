"""Tests for bounded, shell-free Git changed-path selection."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from yaga.changes import git as change_git
from yaga.changes.models import (
    MAX_CHANGED_PATH_BYTES,
    MAX_CHANGED_PATH_COMPONENTS,
    MAX_CHANGED_PATHS,
)
from yaga.errors import GitError


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def init_repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.email", "yaga@example.invalid")
    git(path, "config", "user.name", "YAGA Tests")


def commit_paths(
    repository: Path,
    message: str,
    updates: dict[str, str | None],
) -> str:
    for relative_path, content in updates.items():
        path = repository / relative_path
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "--quiet", "--allow-empty", "-m", message)
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    init_repository(tmp_path)
    root = commit_paths(tmp_path, "chore: initialize", {"README.md": "root\n"})
    return tmp_path, root


def test_two_dot_and_three_dot_use_the_exact_comparison_commit(
    repository: tuple[Path, str],
) -> None:
    path, common_base = repository
    git(path, "switch", "--quiet", "-c", "feature")
    feature_head = commit_paths(path, "feat: add feature", {"feature.txt": "feature\n"})
    git(path, "switch", "--quiet", "main")
    main_head = commit_paths(path, "fix: advance main", {"main.txt": "main\n"})

    two_dot = change_git.read_changed_paths(path, f"{main_head}..{feature_head}")
    three_dot = change_git.read_changed_paths(path, f"{main_head}...{feature_head}")

    assert two_dot.base_sha == main_head
    assert two_dot.head_sha == feature_head
    assert two_dot.comparison_sha == main_head
    assert two_dot.paths == ("feature.txt", "main.txt")
    assert three_dot.base_sha == main_head
    assert three_dot.head_sha == feature_head
    assert three_dot.comparison_sha == common_base
    assert three_dot.paths == ("feature.txt",)


def test_empty_addition_deletion_and_rename_paths_are_exact(
    repository: tuple[Path, str],
) -> None:
    path, _ = repository
    base = commit_paths(
        path,
        "feat: add files",
        {
            "old.txt": "rename me\n",
            "removed.txt": "remove me\n",
        },
    )

    empty = change_git.read_changed_paths(path, f"{base}..{base}")
    assert empty.paths == ()

    git(path, "mv", "old.txt", "renamed.txt")
    head = commit_paths(
        path,
        "feat: change files",
        {
            "added.txt": "added\n",
            "removed.txt": None,
        },
    )
    selected = change_git.read_changed_paths(path, f"{base}..{head}")

    assert selected.paths == (
        "added.txt",
        "old.txt",
        "removed.txt",
        "renamed.txt",
    )


def test_repository_config_cannot_hide_a_changed_gitlink(
    repository: tuple[Path, str],
) -> None:
    path, first_target = repository
    second_target = commit_paths(path, "chore: add target commit", {})
    git(
        path,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{first_target},vendor/dependency",
    )
    git(path, "commit", "--quiet", "-m", "feat: add dependency gitlink")
    base = git(path, "rev-parse", "HEAD")
    git(
        path,
        "update-index",
        "--cacheinfo",
        f"160000,{second_target},vendor/dependency",
    )
    git(path, "commit", "--quiet", "-m", "feat: update dependency gitlink")
    head = git(path, "rev-parse", "HEAD")
    git(path, "config", "diff.ignoreSubmodules", "all")

    selected = change_git.read_changed_paths(path, f"{base}..{head}")

    assert selected.paths == ("vendor/dependency",)


def test_legacy_grafts_cannot_change_a_linked_worktree_merge_base(
    repository: tuple[Path, str],
) -> None:
    path, common_base = repository
    git(path, "switch", "--quiet", "-c", "feature")
    feature_head = commit_paths(
        path,
        "feat: add feature source",
        {"src/change.py": "VALUE = 1\n"},
    )
    git(path, "switch", "--quiet", "main")
    base_head = commit_paths(
        path,
        "feat: add equivalent base source",
        {"src/change.py": "VALUE = 1\n"},
    )
    linked = path.with_name(f"{path.name}-linked")
    git(path, "worktree", "add", "--quiet", "--detach", str(linked), feature_head)
    common_git_dir = Path(git(linked, "rev-parse", "--git-common-dir"))
    if not common_git_dir.is_absolute():
        common_git_dir = linked / common_git_dir
    common_git_dir = common_git_dir.resolve()
    grafts = common_git_dir / "info" / "grafts"
    grafts.touch()

    selected = change_git.read_changed_paths(linked, f"{base_head}...{feature_head}")

    assert selected.comparison_sha == common_base
    assert selected.paths == ("src/change.py",)

    grafts.write_text(f"{feature_head} {base_head}\n", encoding="ascii")
    assert git(linked, "merge-base", "--all", base_head, feature_head) == base_head
    with pytest.raises(GitError, match="legacy graft overlays"):
        change_git.read_changed_paths(linked, f"{base_head}...{feature_head}")


def test_common_git_directory_resolves_relative_to_the_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common_git_dir = tmp_path / ".git"
    common_git_dir.mkdir()
    captured: list[list[str]] = []

    def fake_run(command: list[str], *, stdout_limit: int) -> change_git._ProcessResult:
        captured.append(command)
        assert stdout_limit == change_git._MAX_GIT_COMMON_DIR_BYTES
        return change_git._ProcessResult(0, b".git\n", b"", False, False, False)

    monkeypatch.setattr(change_git, "_run_git", fake_run)

    assert change_git._resolve_common_git_directory(tmp_path, "git") == common_git_dir.resolve()
    assert captured == [["git", "-C", str(tmp_path), "rev-parse", "--git-common-dir"]]


@pytest.mark.parametrize(
    "revision_range",
    [
        "",
        "HEAD",
        "..HEAD",
        "HEAD..",
        "HEAD....main",
        "HEAD..main..tip",
        "--all..HEAD",
        "HEAD..--all",
        "HEAD main..tip",
        "HEAD\n..tip",
        "HEAD\u202e..tip",
        "HEAD^@..tip",
        "HEAD..tip^!",
        "x" * (change_git.MAX_REVISION_CHARS + 1),
    ],
)
def test_range_requires_one_safe_exact_two_dot_or_three_dot_expression(
    revision_range: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(GitError, match="(revision range|changed paths|range endpoints)"):
        change_git.read_changed_paths(tmp_path, revision_range)


def test_missing_repository_git_and_revision_fail_closed(
    repository: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, head = repository
    with pytest.raises(GitError, match="directory does not exist"):
        change_git.read_changed_paths(tmp_path / "missing", "HEAD..HEAD")

    with monkeypatch.context() as context:
        context.setattr(change_git.shutil, "which", lambda _name: None)
        with pytest.raises(GitError, match="git executable was not found"):
            change_git.read_changed_paths(path, "HEAD..HEAD")

    with pytest.raises(GitError, match="could not resolve range base") as raised:
        change_git.read_changed_paths(path, f"missing-ref..{head}")
    assert "\n" not in str(raised.value)


def test_git_executable_must_be_absolute_and_outside_the_repository(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    with monkeypatch.context() as context:
        context.setattr(change_git.shutil, "which", lambda _name: "tools/git")
        with pytest.raises(GitError, match="path must be absolute"):
            change_git.read_changed_paths(path, "HEAD..HEAD")

    repository_git = path / "tools" / "git"
    repository_git.parent.mkdir()
    repository_git.write_bytes(b"untrusted")
    with monkeypatch.context() as context:
        context.setattr(change_git.shutil, "which", lambda _name: str(repository_git.resolve()))
        with pytest.raises(GitError, match="inside the repository"):
            change_git.read_changed_paths(path, "HEAD..HEAD")


def test_changed_paths_reject_a_shallow_repository(
    repository: tuple[Path, str],
) -> None:
    path, _ = repository
    commit_paths(path, "feat: second commit", {"second.txt": "second\n"})
    shallow = path.with_name(f"{path.name}-shallow")
    subprocess.run(
        ["git", "clone", "--depth=1", "--no-local", str(path), str(shallow)],
        check=True,
        capture_output=True,
    )

    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    with pytest.raises(GitError, match="non-shallow repository with complete history"):
        change_git.read_changed_paths(shallow, "HEAD..HEAD")


def test_three_dot_rejects_unrelated_history(repository: tuple[Path, str]) -> None:
    path, root = repository
    tree = git(path, "show", "-s", "--format=%T", root)
    unrelated = git(path, "commit-tree", tree, "-m", "unrelated root")

    with pytest.raises(GitError, match="could not determine one merge base"):
        change_git.read_changed_paths(path, f"{root}...{unrelated}")


def test_three_dot_rejects_ambiguous_merge_bases(repository: tuple[Path, str]) -> None:
    path, _ = repository
    git(path, "switch", "--quiet", "-c", "left")
    left = commit_paths(path, "feat: left", {"left.txt": "left\n"})
    git(path, "switch", "--quiet", "main")
    right = commit_paths(path, "feat: right", {"right.txt": "right\n"})
    left_tree = git(path, "show", "-s", "--format=%T", left)
    right_tree = git(path, "show", "-s", "--format=%T", right)
    first_merge = git(
        path,
        "commit-tree",
        left_tree,
        "-p",
        left,
        "-p",
        right,
        "-m",
        "merge left first",
    )
    second_merge = git(
        path,
        "commit-tree",
        right_tree,
        "-p",
        right,
        "-p",
        left,
        "-m",
        "merge right first",
    )

    assert set(git(path, "merge-base", "--all", first_merge, second_merge).splitlines()) == {
        left,
        right,
    }
    with pytest.raises(GitError, match="exactly one merge base"):
        change_git.read_changed_paths(path, f"{first_merge}...{second_merge}")


@pytest.mark.parametrize("object_id_length", [40, 64])
def test_identity_parser_accepts_lowercase_sha_formats(object_id_length: int) -> None:
    identity = b"a" * object_id_length

    assert change_git._parse_single_identity(identity + b"\n", label="commit") == identity.decode()


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"a" * 39 + b"\n",
        b"a" * 65 + b"\n",
        b"A" * 40 + b"\n",
        b"g" * 40 + b"\n",
        b"a" * 40 + b"\n" + b"b" * 40 + b"\n",
    ],
)
def test_identity_parser_rejects_malformed_or_multiple_values(output: bytes) -> None:
    with pytest.raises(GitError, match="malformed commit identity"):
        change_git._parse_single_identity(output, label="commit")


def test_path_parser_sorts_exact_paths() -> None:
    assert change_git._parse_changed_paths(b"z.txt\0a.txt\0") == (
        "a.txt",
        "z.txt",
    )


def test_path_parser_rejects_duplicate_native_records() -> None:
    with pytest.raises(GitError, match="duplicate changed path"):
        change_git._parse_changed_paths(b"same.txt\0same.txt\0")


def test_path_parser_accepts_public_path_and_count_boundaries() -> None:
    longest = "x" * MAX_CHANGED_PATH_BYTES
    deepest = "/".join("x" for _ in range(MAX_CHANGED_PATH_COMPONENTS))
    paths = tuple(f"files/{index:04}.txt" for index in range(MAX_CHANGED_PATHS))

    assert change_git._parse_changed_paths(longest.encode() + b"\0") == (longest,)
    assert change_git._parse_changed_paths(deepest.encode() + b"\0") == (deepest,)
    output = b"\0".join(path.encode() for path in reversed(paths)) + b"\0"
    assert change_git._parse_changed_paths(output) == paths


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"path.txt", "incomplete"),
        (b"\0", "empty changed path"),
        (b"bad-\xff\0", "not valid UTF-8"),
        (b"/absolute.txt\0", "non-canonical"),
        (b"C:/drive.txt\0", "non-canonical"),
        (b"parent/../escape.txt\0", "non-canonical"),
        (b"dot/./path.txt\0", "non-canonical"),
        (b"duplicate//separator.txt\0", "non-canonical"),
        (b"back\\slash.txt\0", "non-canonical"),
        (b"control\npath.txt\0", "non-canonical"),
        ("bidi\u202epath.txt\0".encode(), "non-canonical"),
        (("x" * (MAX_CHANGED_PATH_BYTES + 1) + "\0").encode(), "non-canonical"),
        (
            ("/".join("x" for _ in range(MAX_CHANGED_PATH_COMPONENTS + 1)) + "\0").encode(),
            "non-canonical",
        ),
    ],
)
def test_path_parser_rejects_malformed_utf8_and_hostile_paths(
    output: bytes,
    message: str,
) -> None:
    with pytest.raises(GitError, match=message):
        change_git._parse_changed_paths(output)


def test_path_parser_rejects_raw_path_count_before_deduplication() -> None:
    output = b"same.txt\0" * (MAX_CHANGED_PATHS + 1)

    with pytest.raises(GitError, match=rf"hard {MAX_CHANGED_PATHS}-path limit"):
        change_git._parse_changed_paths(output)


def test_git_diff_output_is_stopped_at_the_hard_byte_limit(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, base = repository
    head = commit_paths(path, "feat: add long path", {"x" * 100: "content\n"})
    monkeypatch.setattr(change_git, "MAX_GIT_DIFF_BYTES", 32)

    with pytest.raises(GitError, match="hard 32-byte limit"):
        change_git.read_changed_paths(path, f"{base}..{head}")


def test_diff_command_disables_renames_external_diff_and_textconv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    def fake_run(command: list[str], *, stdout_limit: int) -> change_git._ProcessResult:
        captured.append(command)
        assert stdout_limit == change_git.MAX_GIT_DIFF_BYTES
        return change_git._ProcessResult(0, b"", b"", False, False, False)

    monkeypatch.setattr(change_git, "_run_git", fake_run)
    base = "a" * 40
    head = "b" * 40

    assert change_git._read_changed_path_output(Path("repo"), "git", base, head) == ()
    assert captured == [
        [
            "git",
            "-C",
            "repo",
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            base,
            head,
            "--",
        ]
    ]


def test_merge_base_command_requests_all_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []
    base = "a" * 40
    head = "b" * 40

    def fake_run(command: list[str], *, stdout_limit: int) -> change_git._ProcessResult:
        captured.append(command)
        assert stdout_limit == change_git._MAX_MERGE_BASE_BYTES
        return change_git._ProcessResult(
            0,
            ("c" * 40 + "\n").encode(),
            b"",
            False,
            False,
            False,
        )

    monkeypatch.setattr(change_git, "_run_git", fake_run)

    assert change_git._resolve_unique_merge_base(Path("repo"), "git", base, head) == "c" * 40
    assert captured == [["git", "-C", "repo", "merge-base", "--all", base, head]]


def test_process_capture_enforces_streaming_stdout_and_stderr_bounds() -> None:
    for file_descriptor, overflow_attribute, output_attribute in (
        (1, "stdout_overflow", "stdout"),
        (2, "stderr_overflow", "stderr"),
    ):
        result = change_git._run_bounded(
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
        assert result.timed_out is False


def test_git_process_is_killed_at_the_hard_wall_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(change_git, "MAX_GIT_SECONDS", 0.1)

    with pytest.raises(GitError, match="hard 0.1-second limit"):
        change_git._run_git(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout_limit=16,
        )


def test_git_errors_are_sanitized_bounded_and_single_line() -> None:
    detail = change_git._safe_git_error(("bad\u202e ref\n::error::" + "x" * 5000).encode("utf-8"))

    assert "\n" not in detail
    assert "\u202e" not in detail
    assert len(detail) <= 1000


def test_git_environment_is_allowlisted_and_disables_fetch_and_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_ASKPASS",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_VALUE_0",
        "GIT_DIR",
        "GIT_EXEC_PATH",
        "GIT_EXTERNAL_DIFF",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PAGER",
        "GIT_PREFIX",
        "GIT_SSH_COMMAND",
        "GIT_TRACE",
        "GIT_TRACE2_EVENT",
        "GIT_WORK_TREE",
        "HOME",
        "LD_PRELOAD",
        "PAGER",
        "PATH",
        "PYTHONPATH",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.setenv(name, "hostile")
    monkeypatch.setenv("SYSTEMROOT", "system-root")

    environment = change_git._git_environment()

    assert environment["SYSTEMROOT"] == "system-root"
    expected_names = {
        "GCM_INTERACTIVE",
        "GIT_ATTR_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_SYSTEM",
        "GIT_NO_LAZY_FETCH",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OPTIONAL_LOCKS",
        "GIT_TERMINAL_PROMPT",
        "LC_ALL",
    }
    expected_names.update(change_git._PROCESS_ENVIRONMENT_ALLOWLIST & change_git.os.environ.keys())
    assert set(environment) == expected_names
    assert environment["GIT_CONFIG_GLOBAL"] == change_git.os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_SYSTEM"] == change_git.os.devnull
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
