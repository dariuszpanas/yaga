"""Tests for bounded, shell-free committed-path Git selection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from yaga.errors import GitError, InputError
from yaga.git import GitRepository, ProcessResult
from yaga.paths import git as path_git


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


def test_reads_exact_committed_paths_and_ignores_checkout_state(
    repository: tuple[Path, str],
) -> None:
    path, head = repository
    (path / "README.md").write_text("dirty\n", encoding="utf-8")
    (path / "staged.txt").write_text("staged\n", encoding="utf-8")
    git(path, "add", "staged.txt")
    (path / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    selection = path_git.read_path_selection(path, "HEAD")

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

    selection = path_git.read_path_selection(bare, head)

    assert selection.repository == bare.resolve()
    assert selection.commit_sha == head
    assert selection.paths == ("README.md", "src/app.py")


def test_repository_subdirectory_still_selects_the_full_outer_tree(
    repository: tuple[Path, str],
) -> None:
    path, head = repository

    selection = path_git.read_path_selection(path / "src", "HEAD")

    assert selection.repository == (path / "src").resolve()
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

    selection = path_git.read_path_selection(shallow, "HEAD")

    assert selection.commit_sha == head
    assert selection.paths == ("README.md", "second.txt", "src/app.py")


def test_includes_regular_executable_symlink_and_gitlink_leaf_names(
    repository: tuple[Path, str],
) -> None:
    path, gitlink_target = repository
    git(path, "update-index", "--chmod=+x", "src/app.py")
    symlink = (
        subprocess.run(
            ["git", "-C", str(path), "hash-object", "-w", "--stdin"],
            input=b"README.md",
            check=True,
            capture_output=True,
        )
        .stdout.decode("ascii")
        .strip()
    )
    git(path, "update-index", "--add", "--cacheinfo", f"120000,{symlink},docs-link")
    git(
        path,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{gitlink_target},vendor/dependency",
    )
    git(path, "commit", "--quiet", "-m", "feat: add special leaves")

    selection = path_git.read_path_selection(path, "HEAD")

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

    selection = path_git.read_path_selection(path, empty_commit)

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
        "x" * (path_git.MAX_PATH_REVISION_CHARS + 1),
    ],
)
def test_revision_requires_one_safe_exact_commitish(revision: str, tmp_path: Path) -> None:
    with pytest.raises(InputError, match="one bounded, non-option commit-ish"):
        path_git.read_path_selection(tmp_path, revision)


def test_missing_repository_and_revision_fail_closed(
    repository: tuple[Path, str],
    tmp_path: Path,
) -> None:
    path, _ = repository
    with pytest.raises(InputError, match="cannot be resolved safely"):
        path_git.read_path_selection(tmp_path / "missing", "HEAD")

    with pytest.raises(GitError, match="could not resolve") as raised:
        path_git.read_path_selection(path, "missing-ref")
    assert "\n" not in str(raised.value)


def test_resolution_and_enumeration_use_exact_fixed_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.mkdir(exist_ok=True)
    executable = tmp_path.parent / "trusted-git.exe"
    executable.write_bytes(b"placeholder")
    commit_sha = "a" * 40
    tree_sha = "b" * 40
    calls: list[tuple[list[str], int]] = []
    graft_checks: list[str] = []
    events: list[tuple[str, object]] = []
    results = iter(
        [
            ProcessResult(0, f"{commit_sha}\n".encode(), b"", False, False, False),
            ProcessResult(0, f"{tree_sha}\n".encode(), b"", False, False, False),
            ProcessResult(0, b"z.txt\0a.txt\0", b"", False, False, False),
        ]
    )
    selected_repository = GitRepository(tmp_path.resolve(), str(executable.resolve()))

    def fake_run(
        selected: GitRepository,
        arguments: list[str],
        *,
        stdout_limit: int,
    ) -> ProcessResult:
        assert selected == selected_repository
        calls.append((arguments, stdout_limit))
        events.append(("git", tuple(arguments)))
        return next(results)

    def fake_graft_check(selected: GitRepository, *, message: str) -> None:
        assert selected == selected_repository
        graft_checks.append(message)
        events.append(("graft", message))

    monkeypatch.setattr(path_git, "open_repository", lambda _path: selected_repository)
    monkeypatch.setattr(path_git, "run_git", fake_run)
    monkeypatch.setattr(path_git, "require_no_legacy_grafts", fake_graft_check)

    selection = path_git.read_path_selection(tmp_path, "topic")

    assert selection.commit_sha == commit_sha
    assert selection.tree_sha == tree_sha
    assert selection.paths == ("a.txt", "z.txt")
    assert graft_checks == [path_git._GRAFTS_MESSAGE, path_git._GRAFTS_MESSAGE]
    assert calls == [
        (
            ["rev-parse", "--verify", "--end-of-options", "topic^{commit}"],
            path_git._MAX_GIT_IDENTITY_BYTES,
        ),
        (
            ["rev-parse", "--verify", "--end-of-options", f"{commit_sha}^{{tree}}"],
            path_git._MAX_GIT_IDENTITY_BYTES,
        ),
        (
            [
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                "--name-only",
                tree_sha,
                "--",
            ],
            path_git.MAX_GIT_PATH_BYTES,
        ),
    ]
    assert events == [
        ("graft", path_git._GRAFTS_MESSAGE),
        ("git", tuple(calls[0][0])),
        ("git", tuple(calls[1][0])),
        ("git", tuple(calls[2][0])),
        ("graft", path_git._GRAFTS_MESSAGE),
    ]


def test_commit_and_tree_hash_formats_must_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path.parent / "trusted-git.exe"
    executable.write_bytes(b"placeholder")
    results = iter(
        [
            ProcessResult(0, b"a" * 40 + b"\n", b"", False, False, False),
            ProcessResult(0, b"b" * 64 + b"\n", b"", False, False, False),
        ]
    )
    selected_repository = GitRepository(tmp_path.resolve(), str(executable.resolve()))
    monkeypatch.setattr(path_git, "open_repository", lambda _path: selected_repository)
    monkeypatch.setattr(path_git, "run_git", lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(path_git, "require_no_legacy_grafts", lambda *_args, **_kwargs: None)

    with pytest.raises(GitError, match="inconsistent object identity lengths"):
        path_git.read_path_selection(tmp_path, "HEAD")


@pytest.mark.parametrize("call_index", [0, 1, 2])
def test_successful_git_commands_must_not_write_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call_index: int,
) -> None:
    executable = tmp_path.parent / "trusted-git.exe"
    executable.write_bytes(b"placeholder")
    values = [
        ProcessResult(0, b"a" * 40 + b"\n", b"", False, False, False),
        ProcessResult(0, b"b" * 40 + b"\n", b"", False, False, False),
        ProcessResult(0, b"", b"", False, False, False),
    ]
    values[call_index] = ProcessResult(
        values[call_index].returncode,
        values[call_index].stdout,
        b"warning\n",
        False,
        False,
        False,
    )
    results = iter(values)
    selected_repository = GitRepository(tmp_path.resolve(), str(executable.resolve()))
    monkeypatch.setattr(path_git, "open_repository", lambda _path: selected_repository)
    monkeypatch.setattr(path_git, "run_git", lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(path_git, "require_no_legacy_grafts", lambda *_args, **_kwargs: None)

    with pytest.raises(GitError, match="unexpected error output"):
        path_git.read_path_selection(tmp_path, "HEAD")


def test_parser_sorts_paths_and_accepts_raw_policy_findings_at_boundaries() -> None:
    longest = "x" * path_git.MAX_PATH_BYTES
    deepest = "/".join("x" for _ in range(path_git.MAX_PATH_COMPONENTS))
    raw_findings = (
        "C:/windows-drive.txt",
        "back\\slash.txt",
        "colon:name.txt",
        "control\nname.txt",
        "format\u202ename.txt",
    )
    output = b"z.txt\0a.txt\0" + b"".join(value.encode("utf-8") + b"\0" for value in raw_findings)

    assert path_git._parse_paths(output) == tuple(sorted(("a.txt", "z.txt", *raw_findings)))
    assert path_git._parse_paths(longest.encode() + b"\0") == (longest,)
    assert path_git._parse_paths(deepest.encode() + b"\0") == (deepest,)


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"path.txt", "incomplete"),
        (b"\0", "empty committed path"),
        (b"bad-\xff\0", "not valid UTF-8"),
        (b"/absolute.txt\0", "unsafe"),
        (b"parent/../escape.txt\0", "unsafe"),
        (b"dot/./path.txt\0", "unsafe"),
        (b"duplicate//separator.txt\0", "unsafe"),
        (b"trailing/slash/\0", "unsafe"),
        (("x" * (path_git.MAX_PATH_BYTES + 1) + "\0").encode(), "unsafe"),
        (
            ("/".join("x" for _ in range(path_git.MAX_PATH_COMPONENTS + 1)) + "\0").encode(),
            "unsafe",
        ),
    ],
)
def test_parser_rejects_malformed_utf8_and_structurally_unsafe_paths(
    output: bytes,
    message: str,
) -> None:
    with pytest.raises(GitError, match=message):
        path_git._parse_paths(output)


def test_parser_rejects_duplicates_impossible_topology_and_raw_count() -> None:
    with pytest.raises(GitError, match="duplicate committed path"):
        path_git._parse_paths(b"same.txt\0same.txt\0")

    with pytest.raises(GitError, match="impossible committed-path leaf topology"):
        path_git._parse_paths(b"a\0a/b\0")

    output = b"same.txt\0" * (path_git.MAX_PATH_ENTRIES + 1)
    with pytest.raises(GitError, match=rf"hard {path_git.MAX_PATH_ENTRIES}-entry limit"):
        path_git._parse_paths(output)


def test_selection_model_inconsistency_maps_to_git_error(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    monkeypatch.setattr(path_git, "_read_path_output", lambda *_args: ("a", "a/b"))

    with pytest.raises(GitError, match="inconsistent committed-path selection"):
        path_git.read_path_selection(path, "HEAD")


def test_output_is_stopped_at_hard_byte_limit(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    monkeypatch.setattr(path_git, "MAX_GIT_PATH_BYTES", 8)

    with pytest.raises(GitError, match="hard 8-byte limit"):
        path_git.read_path_selection(path, "HEAD")


def test_replacement_refs_cannot_change_selected_paths(
    repository: tuple[Path, str],
) -> None:
    path, original = repository
    replacement = commit_paths(path, "feat: replace contents", {"other.txt": "other\n"})
    git(path, "replace", original, replacement)
    assert git(path, "rev-parse", f"{original}^{{tree}}") == git(
        path, "rev-parse", f"{replacement}^{{tree}}"
    )

    selection = path_git.read_path_selection(path, original)

    assert selection.paths == ("README.md", "src/app.py")


def test_legacy_grafts_cannot_redirect_ancestry_revision(
    repository: tuple[Path, str],
) -> None:
    path, original = repository
    head = commit_paths(path, "feat: add child", {"child.txt": "child\n"})
    original_tree = git(path, "rev-parse", f"{original}^{{tree}}")
    unrelated = git(path, "commit-tree", original_tree, "-m", "unrelated root")
    git(path, "config", "advice.graftFileDeprecated", "false")
    grafts = path / ".git" / "info" / "grafts"
    grafts.write_text(f"{head} {unrelated}\n", encoding="ascii")
    assert git(path, "rev-parse", "HEAD^") == unrelated

    with pytest.raises(GitError, match="rejects legacy graft overlays"):
        path_git.read_path_selection(path, "HEAD^")


def test_graft_created_after_enumeration_fails_final_recheck(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    original_read = path_git._read_path_output

    def read_then_create_graft(selected: GitRepository, tree_sha: str) -> tuple[str, ...]:
        paths = original_read(selected, tree_sha)
        grafts = path / ".git" / "info" / "grafts"
        grafts.write_text("a" * 40 + "\n", encoding="ascii")
        return paths

    monkeypatch.setattr(path_git, "_read_path_output", read_then_create_graft)

    with pytest.raises(GitError, match="rejects legacy graft overlays"):
        path_git.read_path_selection(path, "HEAD")
