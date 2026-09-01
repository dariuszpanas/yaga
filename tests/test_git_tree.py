"""Tests for the shared exact committed-tree identity boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from yaga.errors import GitError, InputError
from yaga.git import (
    CommittedTreeIdentity,
    GitRepository,
    ProcessResult,
    resolve_committed_tree,
)
from yaga.git import tree as committed_tree
from yaga.modes.git import read_mode_selection
from yaga.paths.git import read_path_selection
from yaga.sizes.git import read_blob_sizes
from yaga.trees.git import read_tree_paths


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def init_repository(path: Path, *, object_format: str | None = None) -> str:
    path.mkdir(parents=True, exist_ok=True)
    arguments = ["init", "--quiet", "--initial-branch=main"]
    if object_format is not None:
        arguments.append(f"--object-format={object_format}")
    git(path, *arguments)
    git(path, "config", "user.email", "yaga@example.invalid")
    git(path, "config", "user.name", "YAGA Tests")
    (path / "README.md").write_text("first\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "--quiet", "-m", "chore: initialize")
    return git(path, "rev-parse", "HEAD")


def test_resolver_uses_exact_commands_strict_output_and_graft_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path.parent / "trusted-git.exe"
    executable.write_bytes(b"placeholder")
    repository = GitRepository(tmp_path.resolve(), str(executable.resolve()))
    commit_sha = "a" * 40
    tree_sha = "b" * 40
    results = iter(
        (
            ProcessResult(0, f"{commit_sha}\n".encode(), b"", False, False, False),
            ProcessResult(0, f"{tree_sha}\n".encode(), b"", False, False, False),
        )
    )
    calls: list[tuple[list[str], int]] = []
    events: list[tuple[str, object]] = []

    def fake_run(
        selected: GitRepository,
        arguments: list[str],
        *,
        stdout_limit: int,
    ) -> ProcessResult:
        assert selected == repository
        calls.append((arguments, stdout_limit))
        events.append(("git", tuple(arguments)))
        return next(results)

    def fake_grafts(selected: GitRepository, *, message: str) -> None:
        assert selected == repository
        events.append(("graft", message))

    monkeypatch.setattr(committed_tree, "open_repository", lambda _path: repository)
    monkeypatch.setattr(committed_tree, "run_git", fake_run)
    monkeypatch.setattr(committed_tree, "require_no_legacy_grafts", fake_grafts)

    identity = resolve_committed_tree(tmp_path, "topic")

    assert identity.repository == repository
    assert identity.revision == "topic"
    assert identity.commit_sha == commit_sha
    assert identity.tree_sha == tree_sha
    assert calls == [
        (
            ["rev-parse", "--verify", "--end-of-options", "topic^{commit}"],
            committed_tree._IDENTITY_BYTES,
        ),
        (
            ["rev-parse", "--verify", "--end-of-options", f"{commit_sha}^{{tree}}"],
            committed_tree._IDENTITY_BYTES,
        ),
    ]
    assert events == [
        ("graft", committed_tree._DEFAULT_GRAFTS_MESSAGE),
        ("git", tuple(calls[0][0])),
        ("git", tuple(calls[1][0])),
        ("graft", committed_tree._DEFAULT_GRAFTS_MESSAGE),
    ]
    with pytest.raises(AttributeError):
        identity.__setattr__("tree_sha", "c" * 40)


@pytest.mark.parametrize("call_index", [0, 1])
def test_resolver_rejects_successful_commands_with_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call_index: int,
) -> None:
    repository = GitRepository(tmp_path.resolve(), str((tmp_path / "git.exe").resolve()))
    results = [
        ProcessResult(0, b"a" * 40 + b"\n", b"", False, False, False),
        ProcessResult(0, b"b" * 40 + b"\n", b"", False, False, False),
    ]
    selected = results[call_index]
    results[call_index] = ProcessResult(
        selected.returncode,
        selected.stdout,
        b"warning\n",
        False,
        False,
        False,
    )
    responses = iter(results)
    monkeypatch.setattr(committed_tree, "open_repository", lambda _path: repository)
    monkeypatch.setattr(committed_tree, "run_git", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(
        committed_tree,
        "require_no_legacy_grafts",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(GitError, match="unexpected error output"):
        resolve_committed_tree(tmp_path, "HEAD")


def test_resolver_rejects_mixed_object_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = GitRepository(tmp_path.resolve(), str((tmp_path / "git.exe").resolve()))
    responses = iter(
        (
            ProcessResult(0, b"a" * 40 + b"\n", b"", False, False, False),
            ProcessResult(0, b"b" * 64 + b"\n", b"", False, False, False),
        )
    )
    monkeypatch.setattr(committed_tree, "open_repository", lambda _path: repository)
    monkeypatch.setattr(committed_tree, "run_git", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(
        committed_tree,
        "require_no_legacy_grafts",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(GitError, match="inconsistent object identity lengths"):
        resolve_committed_tree(tmp_path, "HEAD")


@pytest.mark.parametrize(
    "revision",
    [
        "",
        "--all",
        ":vendor",
        "HEAD:vendor",
        "^HEAD",
        "HEAD..main",
        "HEAD main",
        "x" * 513,
    ],
)
def test_resolver_rejects_unsafe_revisions_before_opening_repository(
    revision: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(InputError, match="one bounded, non-option commit-ish"):
        resolve_committed_tree(tmp_path / "missing", revision)


def test_resolver_rejects_colon_selector_that_git_would_resolve_through_gitlink(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    inner_commit = init_repository(repository)
    git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{inner_commit},vendor^{{commit}}",
    )
    git(repository, "commit", "--quiet", "-m", "feat: add gitlink selector trap")
    assert git(repository, "rev-parse", "HEAD:vendor^{commit}") == inner_commit

    with pytest.raises(InputError, match="one bounded, non-option commit-ish"):
        resolve_committed_tree(repository, "HEAD:vendor")


def test_identity_constructor_and_matching_reject_forged_or_mismatched_values(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    init_repository(repository)
    identity = resolve_committed_tree(repository, "HEAD")

    with pytest.raises(ValueError, match="shared resolver"):
        CommittedTreeIdentity(
            identity.repository,
            identity.revision,
            identity.commit_sha,
            identity.tree_sha,
            object(),
        )
    with pytest.raises(InputError, match="requested revision"):
        read_tree_paths(repository, identity.commit_sha, identity=identity)

    other = tmp_path / "other"
    init_repository(other)
    with pytest.raises(InputError, match="requested repository"):
        read_tree_paths(other, "HEAD", identity=identity)


def test_one_identity_survives_ref_movement_across_all_provider_enumerators(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    original_commit = init_repository(repository)
    identity = resolve_committed_tree(repository, "HEAD")

    (repository / "second.txt").write_text("second\n", encoding="utf-8")
    git(repository, "add", "second.txt")
    git(repository, "commit", "--quiet", "-m", "feat: advance ref")
    assert git(repository, "rev-parse", "HEAD") != original_commit

    tree = read_tree_paths(repository, "HEAD", identity=identity)
    paths = read_path_selection(repository, "HEAD", identity=identity)
    sizes = read_blob_sizes(repository, "HEAD", identity=identity)
    modes = read_mode_selection(repository, "HEAD", identity=identity)

    assert tree.revision == paths.revision == sizes.revision == modes.revision == "HEAD"
    assert tree.commit_sha == paths.commit_sha == sizes.commit_sha == modes.commit_sha
    assert tree.commit_sha == original_commit
    assert tree.tree_sha == paths.tree_sha == sizes.tree_sha == modes.tree_sha
    assert tree.paths == paths.paths == ("README.md",)
    assert tuple(item.path for item in sizes.blobs) == ("README.md",)
    assert tuple(item.path for item in modes.entries) == ("README.md",)


def test_resolver_and_enumerators_accept_sha256_repositories(tmp_path: Path) -> None:
    repository = tmp_path / "sha256"
    try:
        commit_sha = init_repository(repository, object_format="sha256")
    except subprocess.CalledProcessError as error:
        pytest.skip(f"installed Git does not support SHA-256 repositories: {error}")

    identity = resolve_committed_tree(repository, "HEAD")
    assert len(commit_sha) == len(identity.commit_sha) == len(identity.tree_sha) == 64
    assert read_tree_paths(repository, "HEAD", identity=identity).paths == ("README.md",)
    assert read_path_selection(repository, "HEAD", identity=identity).paths == ("README.md",)
    assert len(read_blob_sizes(repository, "HEAD", identity=identity).blobs[0].oid) == 64
    assert len(read_mode_selection(repository, "HEAD", identity=identity).entries[0].oid) == 64


def test_graft_created_during_resolution_fails_final_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    init_repository(repository)
    original = committed_tree._resolve_object

    def resolve_then_graft(
        selected: GitRepository,
        revision: str,
        *,
        object_type: str,
    ) -> str:
        result = original(selected, revision, object_type=object_type)
        if object_type == "tree":
            grafts = repository / ".git" / "info" / "grafts"
            grafts.write_text("a" * 40 + "\n", encoding="ascii")
        return result

    monkeypatch.setattr(committed_tree, "_resolve_object", resolve_then_graft)

    with pytest.raises(GitError, match="rejects legacy graft overlays"):
        resolve_committed_tree(repository, "HEAD")


def test_shared_identity_import_remains_standard_library_only() -> None:
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-S",
            "-c",
            (
                "import sys; from yaga.git import CommittedTreeIdentity, resolve_committed_tree; "
                "assert CommittedTreeIdentity and resolve_committed_tree; "
                "assert not ({'click', 'rich', 'typer'} & set(sys.modules))"
            ),
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
