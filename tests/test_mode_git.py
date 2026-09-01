"""Tests for bounded, shell-free committed-mode Git selection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from yaga.errors import GitError, InputError
from yaga.git import GitRepository, ProcessResult, resolve_committed_tree
from yaga.modes import git as mode_git
from yaga.modes.models import ModeEntry, ModeSelection


def git(repository: Path, *arguments: str, input_data: bytes | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=input_data,
        check=True,
        capture_output=True,
    )
    return completed.stdout.decode("ascii").strip()


def init_repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.email", "yaga@example.invalid")
    git(path, "config", "user.name", "YAGA Tests")


def commit_files(
    repository: Path,
    message: str,
    updates: dict[str, bytes | None],
) -> str:
    for relative_path, content in updates.items():
        path = repository / relative_path
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    git(repository, "add", "--all")
    git(repository, "commit", "--quiet", "--allow-empty", "-m", message)
    return git(repository, "rev-parse", "HEAD")


def _record(
    path: str,
    *,
    mode: str = "100644",
    object_type: str = "blob",
    oid: str = "a" * 40,
) -> bytes:
    return f"{mode} {object_type} {oid}\t{path}".encode() + b"\0"


def _entry_map(selection: ModeSelection) -> dict[str, ModeEntry]:
    return {entry.path: entry for entry in selection.entries}


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    init_repository(tmp_path)
    head = commit_files(
        tmp_path,
        "chore: initialize",
        {"README.md": b"root\n", "script.sh": b"#!/bin/sh\n"},
    )
    return tmp_path, head


def test_reads_exact_committed_modes_and_ignores_checkout_state(
    repository: tuple[Path, str],
) -> None:
    path, head = repository
    (path / "README.md").write_bytes(b"dirty\n")
    (path / "staged.txt").write_bytes(b"staged\n")
    git(path, "add", "staged.txt")
    (path / "untracked.txt").write_bytes(b"untracked\n")

    selection = mode_git.read_mode_selection(path, "HEAD")

    assert selection.repository == path.resolve()
    assert selection.revision == "HEAD"
    assert selection.commit_sha == head
    assert selection.tree_sha == git(path, "rev-parse", f"{head}^{{tree}}")
    assert tuple((entry.path, entry.mode, entry.object_type) for entry in selection.entries) == (
        ("README.md", "100644", "blob"),
        ("script.sh", "100644", "blob"),
    )


def test_reads_regular_executable_symlink_and_gitlink_without_recursing(
    repository: tuple[Path, str],
) -> None:
    path, gitlink_target = repository
    git(path, "update-index", "--chmod=+x", "script.sh")
    symlink_oid = git(path, "hash-object", "-w", "--stdin", input_data=b"README.md")
    git(path, "update-index", "--add", "--cacheinfo", f"120000,{symlink_oid},docs-link")
    git(
        path,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{gitlink_target},vendor/dependency",
    )
    git(path, "commit", "--quiet", "-m", "feat: add special leaves")

    entries = _entry_map(mode_git.read_mode_selection(path, "HEAD"))

    assert tuple(entries) == ("README.md", "docs-link", "script.sh", "vendor/dependency")
    assert entries["README.md"].mode == "100644"
    assert entries["script.sh"].mode == "100755"
    assert entries["docs-link"].mode == "120000"
    assert entries["docs-link"].object_type == "blob"
    assert entries["vendor/dependency"].mode == "160000"
    assert entries["vendor/dependency"].object_type == "commit"
    assert entries["vendor/dependency"].oid == gitlink_target


def test_reads_bare_and_outer_tree_from_repository_subdirectory(
    repository: tuple[Path, str],
    tmp_path: Path,
) -> None:
    path, head = repository
    nested = path / "nested"
    nested.mkdir()
    bare = tmp_path.with_name(f"{tmp_path.name}-bare.git")
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(path), str(bare)],
        check=True,
        capture_output=True,
    )

    nested_selection = mode_git.read_mode_selection(nested, head)
    bare_selection = mode_git.read_mode_selection(bare, head)

    assert nested_selection.repository == nested.resolve()
    assert bare_selection.repository == bare.resolve()
    assert nested_selection.entries == bare_selection.entries


def test_shallow_repository_is_allowed_when_selected_objects_exist(
    repository: tuple[Path, str],
    tmp_path: Path,
) -> None:
    path, _ = repository
    head = commit_files(path, "feat: add second", {"second.txt": b"second\n"})
    shallow = tmp_path.with_name(f"{tmp_path.name}-shallow")
    subprocess.run(
        ["git", "clone", "--quiet", "--depth=1", "--no-local", str(path), str(shallow)],
        check=True,
        capture_output=True,
    )
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"

    selection = mode_git.read_mode_selection(shallow, "HEAD")

    assert selection.commit_sha == head
    assert tuple(entry.path for entry in selection.entries) == (
        "README.md",
        "script.sh",
        "second.txt",
    )


def test_empty_committed_tree_is_accepted(repository: tuple[Path, str]) -> None:
    path, _ = repository
    empty_tree = git(path, "mktree")
    empty_commit = git(path, "commit-tree", empty_tree, "-m", "empty tree")

    selection = mode_git.read_mode_selection(path, empty_commit)

    assert selection.tree_sha == empty_tree
    assert selection.entries == ()


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
        "x" * (mode_git.MAX_MODE_REVISION_CHARS + 1),
    ],
)
def test_revision_requires_one_safe_exact_commitish(revision: str, tmp_path: Path) -> None:
    with pytest.raises(InputError, match="one bounded, non-option commit-ish"):
        mode_git.read_mode_selection(tmp_path, revision)


def test_missing_repository_and_revision_fail_closed(
    repository: tuple[Path, str],
    tmp_path: Path,
) -> None:
    path, _ = repository
    with pytest.raises(InputError, match="cannot be resolved safely"):
        mode_git.read_mode_selection(tmp_path / "missing", "HEAD")

    with pytest.raises(GitError, match="could not resolve") as raised:
        mode_git.read_mode_selection(path, "missing-ref")
    assert "\n" not in str(raised.value)


def test_enumeration_reuses_exact_committed_tree_identity(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    identity = resolve_committed_tree(path, "HEAD")
    blob_oid = "c" * len(identity.commit_sha)
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
        return ProcessResult(
            0,
            _record("z.txt", oid=blob_oid) + _record("a.txt", oid=blob_oid),
            b"",
            False,
            False,
            False,
        )

    def fake_graft_check(selected: GitRepository, *, message: str) -> None:
        assert selected == identity.repository
        graft_checks.append(message)
        events.append(("graft", message))

    monkeypatch.setattr(mode_git, "run_git", fake_run)
    monkeypatch.setattr(mode_git, "require_no_legacy_grafts", fake_graft_check)

    selection = mode_git.read_mode_selection(path, "HEAD", identity=identity)

    assert tuple(entry.path for entry in selection.entries) == ("a.txt", "z.txt")
    assert selection.revision == "HEAD"
    assert graft_checks == [mode_git._GRAFTS_MESSAGE, mode_git._GRAFTS_MESSAGE]
    assert calls == [
        (
            [
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                "--format=%(objectmode)%x20%(objecttype)%x20%(objectname)%x09%(path)",
                identity.tree_sha,
                "--",
            ],
            mode_git.MAX_GIT_MODE_BYTES,
        ),
    ]
    assert events == [
        ("graft", mode_git._GRAFTS_MESSAGE),
        ("git", tuple(calls[0][0])),
        ("graft", mode_git._GRAFTS_MESSAGE),
    ]


def test_successful_git_commands_must_not_write_stderr(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    identity = resolve_committed_tree(path, "HEAD")
    monkeypatch.setattr(
        mode_git,
        "run_git",
        lambda *_args, **_kwargs: ProcessResult(0, b"", b"warning\n", False, False, False),
    )
    monkeypatch.setattr(mode_git, "require_no_legacy_grafts", lambda *_args, **_kwargs: None)

    with pytest.raises(GitError, match="unexpected error output"):
        mode_git.read_mode_selection(path, "HEAD", identity=identity)


def test_parser_accepts_all_modes_sha256_and_structural_policy_characters() -> None:
    oid = "b" * 64
    entries = mode_git._parse_mode_entries(
        _record("z:regular", oid=oid)
        + _record("exec\\name", mode="100755", oid=oid)
        + _record("line\nlink", mode="120000", oid=oid)
        + _record("vendor/dependency", mode="160000", object_type="commit", oid=oid),
        identity_length=64,
    )

    assert tuple((entry.path, entry.mode, entry.object_type, entry.oid) for entry in entries) == (
        ("exec\\name", "100755", "blob", oid),
        ("line\nlink", "120000", "blob", oid),
        ("vendor/dependency", "160000", "commit", oid),
        ("z:regular", "100644", "blob", oid),
    )


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (_record("path")[:-1], "incomplete"),
        (b"\0", "empty committed-mode record"),
        (b"100644 blob " + b"a" * 40 + b" path\0", "malformed committed-mode record"),
        (b"100644 blob " + b"a" * 40 + b"\t\0", "malformed committed-mode record"),
        (b"100644  blob " + b"a" * 40 + b"\tpath\0", "metadata"),
        (b"100644 bl\xffb " + b"a" * 40 + b"\tpath\0", "non-ASCII"),
        (_record("path", mode="040000", object_type="tree"), "unsupported"),
        (_record("path", mode="100644", object_type="commit"), "incompatible"),
        (_record("path", mode="160000", object_type="blob"), "incompatible"),
        (_record("path", oid="A" * 40), "identity"),
        (_record("path", oid="a" * 39), "identity"),
        (_record("path", oid="a" * 64), "mixed-format"),
        (b"100644 blob " + b"a" * 40 + b"\tbad-\xff\0", "valid UTF-8"),
        (_record("/absolute"), "non-canonical"),
        (_record("parent/../escape"), "non-canonical"),
        (_record("dot/./path"), "non-canonical"),
        (_record("duplicate//separator"), "non-canonical"),
        (_record("trailing/slash/"), "non-canonical"),
        (_record("x" * (mode_git.MAX_MODE_PATH_BYTES + 1)), "non-canonical"),
        (
            _record("/".join("x" for _ in range(mode_git.MAX_MODE_PATH_COMPONENTS + 1))),
            "non-canonical",
        ),
        (_record("duplicate") + _record("duplicate"), "duplicate"),
    ],
)
def test_parser_rejects_malformed_or_unsafe_records(output: bytes, message: str) -> None:
    with pytest.raises(GitError, match=message):
        mode_git._parse_mode_entries(output, identity_length=40)


def test_parser_rejects_unsupported_expected_identity_length() -> None:
    with pytest.raises(GitError, match="unsupported object identity length"):
        mode_git._parse_mode_entries(b"", identity_length=39)


def test_parser_checks_raw_record_count_before_duplicates() -> None:
    output = _record("same") * (mode_git.MAX_MODE_ENTRIES + 1)

    with pytest.raises(GitError, match=rf"hard {mode_git.MAX_MODE_ENTRIES}-entry limit"):
        mode_git._parse_mode_entries(output, identity_length=40)


def test_selection_model_inconsistency_maps_to_git_error(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    oid = git(path, "rev-parse", "HEAD:README.md")
    impossible = tuple(
        ModeEntry(path=entry_path, oid=oid, mode="100644", object_type="blob")
        for entry_path in ("a", "a-", "a/b")
    )
    monkeypatch.setattr(mode_git, "_read_mode_output", lambda *_args, **_kwargs: impossible)

    with pytest.raises(GitError, match="inconsistent committed-mode selection"):
        mode_git.read_mode_selection(path, "HEAD")


def test_output_is_stopped_at_hard_byte_limit(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    monkeypatch.setattr(mode_git, "MAX_GIT_MODE_BYTES", 8)

    with pytest.raises(GitError, match="hard 8-byte limit"):
        mode_git.read_mode_selection(path, "HEAD")


def test_replacement_refs_cannot_change_selected_modes(
    repository: tuple[Path, str],
) -> None:
    path, original = repository
    git(path, "update-index", "--chmod=+x", "script.sh")
    git(path, "commit", "--quiet", "-m", "feat: replace mode")
    replacement = git(path, "rev-parse", "HEAD")
    git(path, "replace", original, replacement)
    assert git(path, "ls-tree", original, "script.sh").startswith("100755")

    selection = mode_git.read_mode_selection(path, original)

    assert _entry_map(selection)["script.sh"].mode == "100644"


def test_legacy_grafts_cannot_redirect_ancestry_revision(
    repository: tuple[Path, str],
) -> None:
    path, original = repository
    head = commit_files(path, "feat: add child", {"child.txt": b"child\n"})
    original_tree = git(path, "rev-parse", f"{original}^{{tree}}")
    unrelated = git(path, "commit-tree", original_tree, "-m", "unrelated root")
    git(path, "config", "advice.graftFileDeprecated", "false")
    grafts = path / ".git" / "info" / "grafts"
    grafts.write_text(f"{head} {unrelated}\n", encoding="ascii")
    assert git(path, "rev-parse", "HEAD^") == unrelated

    with pytest.raises(GitError, match="rejects legacy graft overlays"):
        mode_git.read_mode_selection(path, "HEAD^")


def test_graft_created_after_enumeration_fails_final_recheck(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    original_read = mode_git._read_mode_output

    def read_then_create_graft(
        selected: GitRepository,
        tree_sha: str,
        *,
        identity_length: int,
    ) -> tuple[ModeEntry, ...]:
        entries = original_read(selected, tree_sha, identity_length=identity_length)
        grafts = path / ".git" / "info" / "grafts"
        grafts.write_text("a" * 40 + "\n", encoding="ascii")
        return entries

    monkeypatch.setattr(mode_git, "_read_mode_output", read_then_create_graft)

    with pytest.raises(GitError, match="rejects legacy graft overlays"):
        mode_git.read_mode_selection(path, "HEAD")
