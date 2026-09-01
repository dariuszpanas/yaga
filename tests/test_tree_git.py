"""Tests for bounded, shell-free committed-tree Git selection."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from yaga.errors import GitError, InputError
from yaga.trees import git as tree_git


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
    head = commit_paths(
        tmp_path,
        "chore: initialize",
        {"README.md": "root\n", "src/app.py": "VALUE = 1\n"},
    )
    return tmp_path, head


def test_reads_exact_committed_tree_and_ignores_checkout_state(
    repository: tuple[Path, str],
) -> None:
    path, head = repository
    (path / "README.md").write_text("dirty\n", encoding="utf-8")
    (path / "staged.txt").write_text("staged\n", encoding="utf-8")
    git(path, "add", "staged.txt")
    (path / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    selection = tree_git.read_tree_paths(path, "HEAD")

    assert selection.repository == path.resolve()
    assert selection.revision == "HEAD"
    assert selection.commit_sha == head
    assert selection.tree_sha == git(path, "rev-parse", f"{head}^{{tree}}")
    assert selection.paths == ("README.md", "src/app.py")


def test_reads_bare_repository(repository: tuple[Path, str], tmp_path: Path) -> None:
    path, head = repository
    bare = tmp_path.with_name(f"{tmp_path.name}-bare.git")
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(path), str(bare)],
        check=True,
        capture_output=True,
    )

    selection = tree_git.read_tree_paths(bare, head)

    assert selection.repository == bare.resolve()
    assert selection.commit_sha == head
    assert selection.paths == ("README.md", "src/app.py")


def test_shallow_repository_is_allowed_when_selected_objects_exist(
    repository: tuple[Path, str],
    tmp_path: Path,
) -> None:
    path, _ = repository
    head = commit_paths(path, "feat: add second", {"second.txt": "second\n"})
    shallow = tmp_path.with_name(f"{tmp_path.name}-shallow")
    subprocess.run(
        ["git", "clone", "--quiet", "--depth=1", "--no-local", str(path), str(shallow)],
        check=True,
        capture_output=True,
    )
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"

    selection = tree_git.read_tree_paths(shallow, "HEAD")

    assert selection.commit_sha == head
    assert selection.paths == ("README.md", "second.txt", "src/app.py")


def test_tree_includes_symlink_and_gitlink_leaf_names(
    repository: tuple[Path, str],
) -> None:
    path, gitlink_target = repository
    symlink_blob = git(path, "hash-object", "-w", "--stdin")
    git(path, "update-index", "--add", "--cacheinfo", f"120000,{symlink_blob},docs-link")
    git(
        path,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{gitlink_target},vendor/dependency",
    )
    git(path, "commit", "--quiet", "-m", "feat: add special leaves")

    selection = tree_git.read_tree_paths(path, "HEAD")

    assert selection.paths == (
        "README.md",
        "docs-link",
        "src/app.py",
        "vendor/dependency",
    )


def test_empty_committed_tree_is_accepted(repository: tuple[Path, str]) -> None:
    path, _ = repository
    empty_tree = git(path, "mktree")
    empty_commit = git(path, "commit-tree", empty_tree, "-m", "empty tree")

    selection = tree_git.read_tree_paths(path, empty_commit)

    assert selection.tree_sha == empty_tree
    assert selection.paths == ()


@pytest.mark.parametrize(
    "revision",
    [
        "",
        "--all",
        "HEAD..main",
        "HEAD...main",
        "^HEAD",
        "HEAD^@",
        "HEAD^!",
        "HEAD^-",
        "HEAD^-2",
        "HEAD main",
        "HEAD\nmain",
        "HEAD\u202emain",
        "x" * (tree_git.MAX_TREE_REVISION_CHARS + 1),
    ],
)
def test_revision_requires_one_safe_exact_commitish(revision: str, tmp_path: Path) -> None:
    with pytest.raises(InputError, match="one bounded, non-option commit-ish"):
        tree_git.read_tree_paths(tmp_path, revision)


def test_missing_repository_git_and_revision_fail_closed(
    repository: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    with pytest.raises(InputError, match="cannot be resolved safely"):
        tree_git.read_tree_paths(tmp_path / "missing", "HEAD")

    with monkeypatch.context() as context:
        context.setattr(tree_git.shutil, "which", lambda _name: None)
        with pytest.raises(GitError, match="git executable was not found"):
            tree_git.read_tree_paths(path, "HEAD")

    with pytest.raises(GitError, match="could not resolve") as raised:
        tree_git.read_tree_paths(path, "missing-ref")
    assert "\n" not in str(raised.value)


def test_git_executable_must_be_absolute_regular_and_outside_repository(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    with monkeypatch.context() as context:
        context.setattr(tree_git.shutil, "which", lambda _name: "tools/git")
        with pytest.raises(GitError, match="path must be absolute"):
            tree_git.read_tree_paths(path, "HEAD")

    repository_git = path / "tools" / "git"
    repository_git.parent.mkdir()
    repository_git.write_bytes(b"untrusted")
    with monkeypatch.context() as context:
        context.setattr(tree_git.shutil, "which", lambda _name: str(repository_git.resolve()))
        with pytest.raises(GitError, match="inside the repository"):
            tree_git.read_tree_paths(path, "HEAD")

    with monkeypatch.context() as context:
        context.setattr(tree_git.shutil, "which", lambda _name: str(path.parent.resolve()))
        with pytest.raises(GitError, match="not a regular file"):
            tree_git.read_tree_paths(path, "HEAD")


def test_git_executable_inside_enclosing_repository_is_rejected_from_subdirectory(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    subdirectory = path / "src"
    repository_git = path / "tools" / "git"
    repository_git.parent.mkdir()
    repository_git.write_bytes(b"untrusted")

    monkeypatch.setattr(tree_git.shutil, "which", lambda _name: str(repository_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        tree_git.read_tree_paths(subdirectory, "HEAD")


def test_git_executable_inside_configless_bare_repository_is_rejected_from_subdirectory(
    repository: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    bare = tmp_path.with_name(f"{tmp_path.name}-configless-bare.git")
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(path), str(bare)],
        check=True,
        capture_output=True,
    )
    (bare / "config").unlink()
    subdirectory = bare / "objects"
    assert Path(git(subdirectory, "rev-parse", "--absolute-git-dir")).resolve() == bare.resolve()
    repository_git = bare / "config.saved"
    repository_git.write_bytes(b"untrusted")

    monkeypatch.setattr(tree_git.shutil, "which", lambda _name: str(repository_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        tree_git.read_tree_paths(subdirectory, "HEAD")


def test_git_executable_inside_separate_git_directory_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    metadata = tmp_path / "separate-metadata"
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
    repository_git = metadata / "repository-git.exe"
    repository_git.write_bytes(b"untrusted")

    monkeypatch.setattr(tree_git.shutil, "which", lambda _name: str(repository_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        tree_git.read_tree_paths(worktree, "HEAD")


def test_git_executable_inside_linked_worktree_common_directory_is_rejected(
    repository: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    linked_worktree = tmp_path.with_name(f"{tmp_path.name}-linked-worktree")
    subprocess.run(
        ["git", "-C", str(path), "worktree", "add", "--quiet", "--detach", str(linked_worktree)],
        check=True,
        capture_output=True,
    )
    repository_git = path / ".git" / "repository-git.exe"
    repository_git.write_bytes(b"untrusted")

    monkeypatch.setattr(tree_git.shutil, "which", lambda _name: str(repository_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        tree_git.read_tree_paths(linked_worktree, "HEAD")


def test_git_metadata_pointer_is_bounded_and_single_line(tmp_path: Path) -> None:
    marker = tmp_path / ".git"
    marker.write_bytes(b"x" * (tree_git._MAX_GIT_METADATA_BYTES + 1))

    with pytest.raises(InputError, match="exceeds"):
        tree_git.read_tree_paths(tmp_path, "HEAD")

    marker.write_text("gitdir: metadata\nsecond-line\n", encoding="utf-8")

    with pytest.raises(InputError, match="one bounded path"):
        tree_git.read_tree_paths(tmp_path, "HEAD")


def test_git_executable_inside_alternate_object_database_is_rejected(
    repository: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    shared = tmp_path.with_name(f"{tmp_path.name}-shared")
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", str(path), str(shared)],
        check=True,
        capture_output=True,
    )
    repository_git = path / ".git" / "objects" / "repository-git.exe"
    repository_git.write_bytes(b"untrusted")

    monkeypatch.setattr(tree_git.shutil, "which", lambda _name: str(repository_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        tree_git.read_tree_paths(shared, "HEAD")


def test_alternate_object_directory_cycles_are_bounded(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory, alternate in ((first, second), (second, first)):
        info = directory / "info"
        info.mkdir(parents=True)
        (info / "alternates").write_text(f"{alternate}\n", encoding="utf-8")

    assert tree_git._git_object_boundaries(first) == (first.resolve(), second.resolve())


@pytest.mark.parametrize("object_id_length", [40, 64])
def test_identity_parser_accepts_lowercase_sha_formats(object_id_length: int) -> None:
    identity = b"a" * object_id_length

    assert tree_git._parse_single_identity(identity + b"\n", label="commit") == identity.decode()


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
        tree_git._parse_single_identity(output, label="commit")


def test_commit_and_tree_resolution_are_exact_and_same_hash_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.mkdir(exist_ok=True)
    executable = tmp_path.parent / "trusted-git.exe"
    executable.write_bytes(b"placeholder")
    commit_sha = "a" * 40
    tree_sha = "b" * 40
    calls: list[tuple[list[str], int]] = []
    results = iter(
        [
            tree_git._ProcessResult(0, f"{commit_sha}\n".encode(), b"", False, False, False),
            tree_git._ProcessResult(0, f"{tree_sha}\n".encode(), b"", False, False, False),
            tree_git._ProcessResult(0, b"z.txt\0a.txt\0", b"", False, False, False),
        ]
    )

    def fake_run(command: list[str], *, stdout_limit: int) -> tree_git._ProcessResult:
        calls.append((command, stdout_limit))
        return next(results)

    monkeypatch.setattr(tree_git.shutil, "which", lambda _name: str(executable.resolve()))
    monkeypatch.setattr(tree_git, "_run_git", fake_run)

    selection = tree_git.read_tree_paths(tmp_path, "topic")

    assert selection.commit_sha == commit_sha
    assert selection.tree_sha == tree_sha
    assert selection.paths == ("a.txt", "z.txt")
    assert calls == [
        (
            [
                str(executable.resolve()),
                "--no-lazy-fetch",
                "-C",
                str(tmp_path.resolve()),
                "rev-parse",
                "--verify",
                "--end-of-options",
                "topic^{commit}",
            ],
            tree_git._MAX_GIT_IDENTITY_BYTES,
        ),
        (
            [
                str(executable.resolve()),
                "--no-lazy-fetch",
                "-C",
                str(tmp_path.resolve()),
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{commit_sha}^{{tree}}",
            ],
            tree_git._MAX_GIT_IDENTITY_BYTES,
        ),
        (
            [
                str(executable.resolve()),
                "--no-lazy-fetch",
                "-C",
                str(tmp_path.resolve()),
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                "--name-only",
                tree_sha,
                "--",
            ],
            tree_git.MAX_GIT_TREE_BYTES,
        ),
    ]


def test_commit_and_tree_hash_formats_must_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path.parent / "trusted-git.exe"
    executable.write_bytes(b"placeholder")
    results = iter(
        [
            tree_git._ProcessResult(0, b"a" * 40 + b"\n", b"", False, False, False),
            tree_git._ProcessResult(0, b"b" * 64 + b"\n", b"", False, False, False),
        ]
    )
    monkeypatch.setattr(tree_git.shutil, "which", lambda _name: str(executable.resolve()))
    monkeypatch.setattr(tree_git, "_run_git", lambda *_args, **_kwargs: next(results))

    with pytest.raises(GitError, match="inconsistent object identity lengths"):
        tree_git.read_tree_paths(tmp_path, "HEAD")


def test_path_parser_sorts_exact_paths_and_accepts_boundaries() -> None:
    longest = "x" * tree_git.MAX_TREE_PATH_BYTES
    deepest = "/".join("x" for _ in range(tree_git.MAX_TREE_PATH_COMPONENTS))
    assert tree_git._parse_tree_paths(b"z.txt\0a.txt\0") == ("a.txt", "z.txt")
    assert tree_git._parse_tree_paths(longest.encode() + b"\0") == (longest,)
    assert tree_git._parse_tree_paths(deepest.encode() + b"\0") == (deepest,)


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"path.txt", "incomplete"),
        (b"\0", "empty tree path"),
        (b"bad-\xff\0", "not valid UTF-8"),
        (b"/absolute.txt\0", "non-canonical"),
        (b"C:/drive.txt\0", "non-canonical"),
        (b"parent/../escape.txt\0", "non-canonical"),
        (b"dot/./path.txt\0", "non-canonical"),
        (b"duplicate//separator.txt\0", "non-canonical"),
        (b"back\\slash.txt\0", "non-canonical"),
        (b"control\npath.txt\0", "non-canonical"),
        ("bidi\u202epath.txt\0".encode(), "non-canonical"),
        (("x" * (tree_git.MAX_TREE_PATH_BYTES + 1) + "\0").encode(), "non-canonical"),
        (
            ("/".join("x" for _ in range(tree_git.MAX_TREE_PATH_COMPONENTS + 1)) + "\0").encode(),
            "non-canonical",
        ),
    ],
)
def test_path_parser_rejects_malformed_utf8_and_hostile_paths(
    output: bytes,
    message: str,
) -> None:
    with pytest.raises(GitError, match=message):
        tree_git._parse_tree_paths(output)


def test_path_parser_rejects_duplicates_and_raw_count_before_deduplication() -> None:
    with pytest.raises(GitError, match="duplicate tree path"):
        tree_git._parse_tree_paths(b"same.txt\0same.txt\0")

    output = b"same.txt\0" * (tree_git.MAX_TREE_PATHS + 1)
    with pytest.raises(GitError, match=rf"hard {tree_git.MAX_TREE_PATHS}-path limit"):
        tree_git._parse_tree_paths(output)


def test_tree_output_is_stopped_at_hard_byte_limit(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    monkeypatch.setattr(tree_git, "MAX_GIT_TREE_BYTES", 8)

    with pytest.raises(GitError, match="hard 8-byte limit"):
        tree_git.read_tree_paths(path, "HEAD")


def test_process_capture_enforces_streaming_stdout_and_stderr_bounds() -> None:
    for file_descriptor, overflow_attribute, output_attribute in (
        (1, "stdout_overflow", "stdout"),
        (2, "stderr_overflow", "stderr"),
    ):
        result = tree_git._run_bounded(
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


def test_git_process_is_killed_at_hard_wall_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tree_git, "MAX_GIT_SECONDS", 0.1)

    with pytest.raises(GitError, match="hard 0.1-second limit"):
        tree_git._run_git(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout_limit=16,
        )


def test_git_errors_are_sanitized_bounded_and_single_line() -> None:
    detail = tree_git._safe_git_error(("bad\u202e ref\n::error::" + "x" * 5000).encode("utf-8"))

    assert "\n" not in detail
    assert "\u202e" not in detail
    assert len(detail) <= 1000


def test_git_environment_is_allowlisted_and_disables_lazy_fetch_and_replacements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_names = (
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
    )
    for name in hostile_names:
        monkeypatch.setenv(name, "hostile")
    monkeypatch.setenv("SYSTEMROOT", "system-root")

    environment = tree_git._git_environment()

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
    expected_names.update(tree_git._PROCESS_ENVIRONMENT_ALLOWLIST & tree_git.os.environ.keys())
    assert set(environment) == expected_names
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_replacement_refs_cannot_change_the_selected_tree(
    repository: tuple[Path, str],
) -> None:
    path, original = repository
    replacement = commit_paths(path, "feat: replace contents", {"other.txt": "other\n"})
    git(path, "replace", original, replacement)
    assert git(path, "rev-parse", f"{original}^{{tree}}") == git(
        path, "rev-parse", f"{replacement}^{{tree}}"
    )

    selection = tree_git.read_tree_paths(path, original)

    assert selection.paths == ("README.md", "src/app.py")


def test_run_bounded_uses_no_shell_devnull_stdin_and_minimal_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Stream:
        def read(self, _size: int) -> bytes:
            return b""

        def close(self) -> None:
            return None

    class Process:
        stdout = Stream()
        stderr = Stream()

        def poll(self) -> int:
            return 0

        def wait(self, *, timeout: float | None = None) -> int:
            captured["timeout"] = timeout
            return 0

        def kill(self) -> None:
            raise AssertionError("completed process must not be killed")

    def fake_popen(command: list[str], **kwargs: object) -> Process:
        captured["command"] = command
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(tree_git.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tree_git, "_git_environment", lambda: {"LC_ALL": "C"})

    result = tree_git._run_bounded(
        ["C:/Git/git.exe", "--version"],
        stdout_limit=10,
        stderr_limit=20,
        timeout_seconds=3.0,
    )

    assert result.returncode == 0
    assert captured["command"] == ["C:/Git/git.exe", "--version"]
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE
    assert captured["env"] == {"LC_ALL": "C"}
    assert captured["shell"] is False
    assert captured["timeout"] == 3.0
