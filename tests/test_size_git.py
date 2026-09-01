"""Tests for bounded committed-tree blob-size selection."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from yaga.errors import GitError, InputError
from yaga.git import GitRepository, ProcessResult
from yaga.sizes import git as size_git
from yaga.sizes.models import BlobEntry, SizeSelection


def git(repository: Path, *arguments: str, input_data: bytes | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        input=input_data,
    )
    return completed.stdout.decode("utf-8").strip()


def init_repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.email", "yaga@example.invalid")
    git(path, "config", "user.name", "YAGA Tests")


def commit_files(repository: Path, message: str, updates: dict[str, bytes | None]) -> str:
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


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "repository"
    init_repository(path)
    commit_files(
        path,
        "chore: initialize",
        {
            "README.md": b"root\n",
            "duplicate-a.bin": b"same",
            "duplicate-b.bin": b"same",
            "script.sh": b"#!/bin/sh\nexit 0\n",
        },
    )
    git(path, "update-index", "--chmod=+x", "script.sh")
    git(path, "commit", "--quiet", "--amend", "--no-edit")
    return path, git(path, "rev-parse", "HEAD")


def _blob_map(selection: SizeSelection) -> dict[str, BlobEntry]:
    return {entry.path: entry for entry in selection.blobs}


def _record(
    path: str,
    *,
    mode: str = "100644",
    object_type: str = "blob",
    oid: str = "a" * 40,
    size: str = "1",
) -> bytes:
    return f"{mode} {object_type} {oid} {size}\t{path}\0".encode()


def test_reads_exact_committed_blob_sizes_and_ignores_checkout_state(
    repository: tuple[Path, str],
) -> None:
    path, head = repository
    (path / "README.md").write_bytes(b"dirty and much larger\n")
    (path / "staged.txt").write_bytes(b"staged\n")
    git(path, "add", "staged.txt")
    (path / "untracked.txt").write_bytes(b"untracked\n")

    selection = size_git.read_blob_sizes(path, "HEAD")

    blobs = _blob_map(selection)
    assert selection.repository == path.resolve()
    assert selection.revision == "HEAD"
    assert selection.commit_sha == head
    assert selection.tree_sha == git(path, "rev-parse", f"{head}^{{tree}}")
    assert tuple(blobs) == ("README.md", "duplicate-a.bin", "duplicate-b.bin", "script.sh")
    assert blobs["README.md"].size == 5
    assert blobs["script.sh"].mode == "100755"
    assert blobs["duplicate-a.bin"].oid == blobs["duplicate-b.bin"].oid
    assert blobs["duplicate-a.bin"].size == blobs["duplicate-b.bin"].size == 4
    assert selection.gitlinks == ()


def test_counts_symlink_target_and_lfs_pointer_bytes_but_not_gitlinks(
    repository: tuple[Path, str],
) -> None:
    path, gitlink_target = repository
    symlink_target = b"docs/README.md"
    symlink_oid = git(path, "hash-object", "-w", "--stdin", input_data=symlink_target)
    lfs_pointer = (
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:" + b"a" * 64 + b"\nsize 999999999\n"
    )
    (path / "asset.bin").write_bytes(lfs_pointer)
    git(path, "add", "asset.bin")
    git(path, "update-index", "--add", "--cacheinfo", f"120000,{symlink_oid},docs-link")
    git(
        path,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{gitlink_target},vendor/dependency",
    )
    git(path, "commit", "--quiet", "-m", "feat: add special leaves")

    selection = size_git.read_blob_sizes(path, "HEAD")

    blobs = _blob_map(selection)
    assert blobs["docs-link"].mode == "120000"
    assert blobs["docs-link"].size == len(symlink_target)
    assert blobs["asset.bin"].size == len(lfs_pointer)
    assert "vendor/dependency" not in blobs
    assert selection.gitlinks == ("vendor/dependency",)


def test_reads_bare_separate_linked_and_alternate_repositories(
    repository: tuple[Path, str],
    tmp_path: Path,
) -> None:
    path, head = repository
    bare = tmp_path / "bare.git"
    separate = tmp_path / "separate"
    separate_metadata = tmp_path / "separate-metadata.git"
    linked = tmp_path / "linked"
    shared = tmp_path / "shared"
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(path), str(bare)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            f"--separate-git-dir={separate_metadata}",
            str(path),
            str(separate),
        ],
        check=True,
        capture_output=True,
    )
    git(path, "worktree", "add", "--quiet", "--detach", str(linked), head)
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", str(path), str(shared)],
        check=True,
        capture_output=True,
    )

    selections = tuple(
        size_git.read_blob_sizes(candidate, head) for candidate in (bare, separate, linked, shared)
    )

    assert all(selection.commit_sha == head for selection in selections)
    assert all(
        tuple(entry.path for entry in selection.blobs)
        == ("README.md", "duplicate-a.bin", "duplicate-b.bin", "script.sh")
        for selection in selections
    )


def test_shallow_repository_is_allowed_when_selected_objects_exist(
    repository: tuple[Path, str],
    tmp_path: Path,
) -> None:
    path, _ = repository
    head = commit_files(path, "feat: add second", {"second.txt": b"second\n"})
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth=1", "--no-local", str(path), str(shallow)],
        check=True,
        capture_output=True,
    )
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"

    selection = size_git.read_blob_sizes(shallow, "HEAD")

    assert selection.commit_sha == head
    assert "second.txt" in tuple(entry.path for entry in selection.blobs)


def test_empty_committed_tree_is_accepted(repository: tuple[Path, str]) -> None:
    path, _ = repository
    empty_tree = git(path, "mktree")
    empty_commit = git(path, "commit-tree", empty_tree, "-m", "empty tree")

    selection = size_git.read_blob_sizes(path, empty_commit)

    assert selection.tree_sha == empty_tree
    assert selection.blobs == ()
    assert selection.gitlinks == ()


def test_missing_promised_blob_fails_without_fetching(
    repository: tuple[Path, str],
) -> None:
    path, _ = repository
    commit_files(path, "feat: add promised blob", {"promised.bin": b"unique promised bytes"})
    oid = git(path, "rev-parse", "HEAD:promised.bin")
    git(path, "config", "remote.origin.promisor", "true")
    git(path, "config", "remote.origin.partialclonefilter", "blob:none")
    object_path = path / ".git" / "objects" / oid[:2] / oid[2:]
    object_path.chmod(stat.S_IWRITE)
    object_path.unlink()

    with pytest.raises(GitError, match="could not read blob sizes"):
        size_git.read_blob_sizes(path, "HEAD")


def test_replacement_refs_cannot_change_selected_blob_sizes(
    repository: tuple[Path, str],
) -> None:
    path, original = repository
    replacement = commit_files(
        path,
        "feat: replace contents",
        {"README.md": b"replacement contents are longer\n"},
    )
    git(path, "replace", original, replacement)
    assert git(path, "rev-parse", f"{original}^{{tree}}") == git(
        path, "rev-parse", f"{replacement}^{{tree}}"
    )

    selection = size_git.read_blob_sizes(path, original)

    assert _blob_map(selection)["README.md"].size == 5


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
        size_git.read_blob_sizes(path, "HEAD^")


def test_graft_created_after_blob_enumeration_fails_final_recheck(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    original_read = size_git._read_blob_size_output

    def read_then_create_graft(
        selected: GitRepository,
        tree_sha: str,
        *,
        identity_length: int,
    ) -> tuple[tuple[BlobEntry, ...], tuple[str, ...]]:
        entries = original_read(selected, tree_sha, identity_length=identity_length)
        grafts = path / ".git" / "info" / "grafts"
        grafts.write_text("a" * 40 + "\n", encoding="ascii")
        return entries

    monkeypatch.setattr(size_git, "_read_blob_size_output", read_then_create_graft)

    with pytest.raises(GitError, match="rejects legacy graft overlays"):
        size_git.read_blob_sizes(path, "HEAD")


def test_impossible_leaf_topology_from_git_maps_to_git_error(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    oid = git(path, "rev-parse", "HEAD:README.md")
    size = int(git(path, "cat-file", "-s", oid))
    impossible = tuple(
        BlobEntry(path=blob_path, oid=oid, mode="100644", size=size)
        for blob_path in ("a", "a-", "a/b")
    )
    monkeypatch.setattr(
        size_git,
        "_read_blob_size_output",
        lambda *_args, **_kwargs: (impossible, ()),
    )

    with pytest.raises(GitError, match="inconsistent committed-tree blob selection"):
        size_git.read_blob_sizes(path, "HEAD")


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
        "x" * (size_git.MAX_SIZE_REVISION_CHARS + 1),
    ],
)
def test_revision_requires_one_safe_exact_commitish(revision: str, tmp_path: Path) -> None:
    with pytest.raises(InputError, match="one bounded, non-option commit-ish"):
        size_git.read_blob_sizes(tmp_path, revision)


def test_missing_repository_and_revision_fail_closed(
    repository: tuple[Path, str],
    tmp_path: Path,
) -> None:
    path, _ = repository
    with pytest.raises(InputError, match="cannot be resolved safely"):
        size_git.read_blob_sizes(tmp_path / "missing", "HEAD")

    with pytest.raises(GitError, match="could not resolve") as raised:
        size_git.read_blob_sizes(path, "missing-ref")
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
    blob_oid = "c" * 40
    calls: list[tuple[list[str], int]] = []
    graft_checks: list[str] = []
    events: list[tuple[str, object]] = []
    results = iter(
        [
            ProcessResult(0, f"{commit_sha}\n".encode(), b"", False, False, False),
            ProcessResult(0, f"{tree_sha}\n".encode(), b"", False, False, False),
            ProcessResult(
                0,
                _record("z.txt", oid=blob_oid, size="2") + _record("a.txt", oid=blob_oid, size="2"),
                b"",
                False,
                False,
                False,
            ),
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

    monkeypatch.setattr(size_git, "open_repository", lambda _path: selected_repository)
    monkeypatch.setattr(size_git, "run_git", fake_run)
    monkeypatch.setattr(size_git, "require_no_legacy_grafts", fake_graft_check)

    selection = size_git.read_blob_sizes(tmp_path, "topic")

    assert tuple(entry.path for entry in selection.blobs) == ("a.txt", "z.txt")
    assert selection.blobs[0].oid == selection.blobs[1].oid == blob_oid
    assert graft_checks == [size_git._GRAFTS_MESSAGE, size_git._GRAFTS_MESSAGE]
    assert calls == [
        (
            ["rev-parse", "--verify", "--end-of-options", "topic^{commit}"],
            size_git._MAX_GIT_IDENTITY_BYTES,
        ),
        (
            ["rev-parse", "--verify", "--end-of-options", f"{commit_sha}^{{tree}}"],
            size_git._MAX_GIT_IDENTITY_BYTES,
        ),
        (
            [
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                "--format=%(objectmode)%x20%(objecttype)%x20%(objectname)%x20%(objectsize)%x09%(path)",
                tree_sha,
                "--",
            ],
            size_git.MAX_GIT_SIZE_BYTES,
        ),
    ]
    assert events == [
        ("graft", size_git._GRAFTS_MESSAGE),
        ("git", tuple(calls[0][0])),
        ("git", tuple(calls[1][0])),
        ("git", tuple(calls[2][0])),
        ("graft", size_git._GRAFTS_MESSAGE),
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
    monkeypatch.setattr(size_git, "open_repository", lambda _path: selected_repository)
    monkeypatch.setattr(size_git, "run_git", lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(size_git, "require_no_legacy_grafts", lambda *_args, **_kwargs: None)

    with pytest.raises(GitError, match="inconsistent object identity lengths"):
        size_git.read_blob_sizes(tmp_path, "HEAD")


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
    monkeypatch.setattr(size_git, "open_repository", lambda _path: selected_repository)
    monkeypatch.setattr(size_git, "run_git", lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(size_git, "require_no_legacy_grafts", lambda *_args, **_kwargs: None)

    with pytest.raises(GitError, match="unexpected error output"):
        size_git.read_blob_sizes(tmp_path, "HEAD")


def test_parser_accepts_blob_modes_canonical_boundaries_and_gitlink_sizes() -> None:
    oid = "a" * 40
    blobs, gitlinks = size_git._parse_blob_sizes(
        _record("zero", mode="100644", oid=oid, size="0")
        + _record("exec", mode="100755", oid=oid, size="1")
        + _record(
            "link",
            mode="120000",
            oid=oid,
            size=str(size_git.MAX_SIZE_BYTES),
        )
        + _record("vendor/a", mode="160000", object_type="commit", oid=oid, size="-")
        + _record("vendor/b", mode="160000", object_type="commit", oid=oid, size="123"),
        identity_length=40,
    )

    assert tuple((entry.path, entry.mode, entry.size) for entry in blobs) == (
        ("exec", "100755", 1),
        ("link", "120000", size_git.MAX_SIZE_BYTES),
        ("zero", "100644", 0),
    )
    assert blobs[0].oid == blobs[1].oid == blobs[2].oid == oid
    assert gitlinks == ("vendor/a", "vendor/b")


def test_parser_accepts_sha256_entry_identities() -> None:
    blobs, gitlinks = size_git._parse_blob_sizes(
        _record("asset.bin", oid="b" * 64, size="4"),
        identity_length=64,
    )

    assert blobs[0].oid == "b" * 64
    assert gitlinks == ()


@pytest.mark.parametrize(
    "raw_size",
    [b"", b"-", b"+1", b"00", b"01", b" 1", b"1 ", b"1_0", b"1a"],
)
def test_size_parser_rejects_noncanonical_blob_sizes(raw_size: bytes) -> None:
    with pytest.raises(GitError, match="non-canonical"):
        size_git._parse_canonical_size(raw_size, allow_unavailable=False)


def test_size_parser_rejects_values_above_portable_integer_limit() -> None:
    with pytest.raises(GitError, match="above the hard"):
        size_git._parse_canonical_size(
            str(size_git.MAX_SIZE_BYTES + 1).encode(),
            allow_unavailable=False,
        )


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (_record("path")[:-1], "incomplete"),
        (b"\0", "empty blob-size record"),
        (b"100644 blob " + b"a" * 40 + b" 1 path\0", "malformed blob-size record"),
        (b"100644 blob " + b"a" * 40 + b" 1\t\0", "malformed blob-size record"),
        (b"100644  blob " + b"a" * 40 + b" 1\tpath\0", "metadata"),
        (b"100644 bl\xffb " + b"a" * 40 + b" 1\tpath\0", "non-ASCII"),
        (_record("path", mode="040000", object_type="tree", size="-"), "unsupported"),
        (_record("path", mode="100644", object_type="commit"), "incompatible"),
        (_record("path", mode="160000", object_type="blob"), "incompatible"),
        (_record("path", oid="A" * 40), "identity"),
        (_record("path", oid="a" * 39), "identity"),
        (_record("path", oid="a" * 64), "mixed-format"),
        (b"100644 blob " + b"a" * 40 + b" 1\tbad-\xff\0", "valid UTF-8"),
        (_record("/absolute"), "non-canonical"),
        (_record("parent/../escape"), "non-canonical"),
        (_record("back\\slash"), "non-canonical"),
        (_record("control\npath"), "non-canonical"),
        (_record("x" * (size_git.MAX_SIZE_PATH_BYTES + 1)), "non-canonical"),
        (
            _record("/".join("x" for _ in range(size_git.MAX_SIZE_PATH_COMPONENTS + 1))),
            "non-canonical",
        ),
        (_record("duplicate") + _record("duplicate"), "duplicate"),
    ],
)
def test_parser_rejects_malformed_or_unsafe_records(output: bytes, message: str) -> None:
    with pytest.raises(GitError, match=message):
        size_git._parse_blob_sizes(output, identity_length=40)


def test_parser_rejects_unsupported_expected_identity_length() -> None:
    with pytest.raises(GitError, match="unsupported object identity length"):
        size_git._parse_blob_sizes(b"", identity_length=39)


def test_size_parser_bounds_decimal_conversion_work() -> None:
    with pytest.raises(GitError, match="non-canonical"):
        size_git._parse_canonical_size(b"9" * 10_000, allow_unavailable=False)


def test_parser_checks_raw_record_count_before_duplicates() -> None:
    output = _record("same") * (size_git.MAX_SIZE_ENTRIES + 1)

    with pytest.raises(GitError, match=rf"hard {size_git.MAX_SIZE_ENTRIES}-entry limit"):
        size_git._parse_blob_sizes(output, identity_length=40)


def test_blob_size_output_is_stopped_at_hard_byte_limit(
    repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = repository
    monkeypatch.setattr(size_git, "MAX_GIT_SIZE_BYTES", 8)

    with pytest.raises(GitError, match="hard 8-byte limit"):
        size_git.read_blob_sizes(path, "HEAD")
