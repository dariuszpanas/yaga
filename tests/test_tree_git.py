"""Tests for bounded, shell-free committed-tree Git selection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from yaga.errors import GitError, InputError
from yaga.git import GitRepository, ProcessResult, resolve_committed_tree
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
        ":vendor",
        "HEAD:vendor",
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


def test_missing_repository_and_revision_fail_closed(
    repository: tuple[Path, str],
    tmp_path: Path,
) -> None:
    path, _ = repository
    with pytest.raises(InputError, match="cannot be resolved safely"):
        tree_git.read_tree_paths(tmp_path / "missing", "HEAD")

    with pytest.raises(GitError, match="could not resolve") as raised:
        tree_git.read_tree_paths(path, "missing-ref")
    assert "\n" not in str(raised.value)


def test_enumeration_reuses_exact_committed_tree_identity(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    identity = resolve_committed_tree(path, "HEAD")
    calls: list[tuple[list[str], int]] = []
    graft_checks: list[str] = []
    events: list[tuple[str, object]] = []

    def fake_run(
        selected: GitRepository,
        arguments: list[str],
        *,
        stdout_limit: int,
    ) -> ProcessResult:
        assert selected == identity.repository
        calls.append((arguments, stdout_limit))
        events.append(("git", tuple(arguments)))
        return ProcessResult(0, b"z.txt\0a.txt\0", b"", False, False, False)

    def fake_graft_check(selected: GitRepository, *, message: str) -> None:
        assert selected == identity.repository
        graft_checks.append(message)
        events.append(("graft", message))

    monkeypatch.setattr(tree_git, "run_git", fake_run)
    monkeypatch.setattr(tree_git, "require_no_legacy_grafts", fake_graft_check)

    selection = tree_git.read_tree_paths(path, "HEAD", identity=identity)

    assert selection.commit_sha == identity.commit_sha
    assert selection.tree_sha == identity.tree_sha
    assert selection.revision == "HEAD"
    assert selection.paths == ("a.txt", "z.txt")
    assert graft_checks == [tree_git._GRAFTS_MESSAGE, tree_git._GRAFTS_MESSAGE]
    assert calls == [
        (
            [
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                "--name-only",
                identity.tree_sha,
                "--",
            ],
            tree_git.MAX_GIT_TREE_BYTES,
        ),
    ]
    assert events == [
        ("graft", tree_git._GRAFTS_MESSAGE),
        ("git", tuple(calls[0][0])),
        ("graft", tree_git._GRAFTS_MESSAGE),
    ]


def test_successful_git_commands_must_not_write_stderr(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    identity = resolve_committed_tree(path, "HEAD")
    monkeypatch.setattr(
        tree_git,
        "run_git",
        lambda *_args, **_kwargs: ProcessResult(0, b"", b"warning\n", False, False, False),
    )
    monkeypatch.setattr(tree_git, "require_no_legacy_grafts", lambda *_args, **_kwargs: None)

    with pytest.raises(GitError, match="unexpected error output"):
        tree_git.read_tree_paths(path, "HEAD", identity=identity)


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
        tree_git.read_tree_paths(path, "HEAD^")


def test_graft_created_after_tree_enumeration_fails_final_recheck(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    original_read = tree_git._read_tree_path_output

    def read_then_create_graft(selected: GitRepository, tree_sha: str) -> tuple[str, ...]:
        paths = original_read(selected, tree_sha)
        grafts = path / ".git" / "info" / "grafts"
        grafts.write_text("a" * 40 + "\n", encoding="ascii")
        return paths

    monkeypatch.setattr(tree_git, "_read_tree_path_output", read_then_create_graft)

    with pytest.raises(GitError, match="rejects legacy graft overlays"):
        tree_git.read_tree_paths(path, "HEAD")
