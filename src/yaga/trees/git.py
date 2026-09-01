"""Bounded, shell-free selection of paths from one committed Git tree."""

from __future__ import annotations

from pathlib import Path

from yaga.errors import GitError, InputError
from yaga.git import (
    CommittedTreeIdentity,
    GitRepository,
    require_no_legacy_grafts,
    resolve_committed_tree,
    run_git,
    safe_git_error,
)
from yaga.git.tree import require_matching_identity
from yaga.trees.models import (
    MAX_TREE_PATH_BYTES,
    MAX_TREE_PATH_COMPONENTS,
    MAX_TREE_PATHS,
    MAX_TREE_REVISION_CHARS,
    TreeSelection,
    _validate_tree_revision,
)
from yaga.trees.models import (
    _validate_tree_path as _validate_model_tree_path,
)

MAX_GIT_TREE_BYTES = 64 * 1024 * 1024
_GRAFTS_MESSAGE = "committed-tree selection rejects legacy graft overlays"


def read_tree_paths(
    repository: Path,
    revision: str,
    *,
    identity: CommittedTreeIdentity | None = None,
) -> TreeSelection:
    """Resolve exactly one commit and return the leaf paths in its committed tree."""
    _validate_revision(revision)
    if identity is None:
        identity = resolve_committed_tree(
            repository,
            revision,
            grafts_message=_GRAFTS_MESSAGE,
        )
    else:
        require_matching_identity(identity, repository=repository, revision=revision)
    repo = identity.repository
    require_no_legacy_grafts(repo, message=_GRAFTS_MESSAGE)
    paths = _read_tree_path_output(repo, identity.tree_sha)
    require_no_legacy_grafts(repo, message=_GRAFTS_MESSAGE)
    return TreeSelection(
        repository=repo.directory,
        revision=identity.revision,
        commit_sha=identity.commit_sha,
        tree_sha=identity.tree_sha,
        paths=paths,
    )


def _validate_revision(revision: str) -> None:
    try:
        _validate_tree_revision(revision)
    except ValueError as error:
        raise InputError(
            "tree revision must be one bounded, non-option commit-ish "
            f"of at most {MAX_TREE_REVISION_CHARS} characters"
        ) from error


def _read_tree_path_output(repo: GitRepository, tree_sha: str) -> tuple[str, ...]:
    result = run_git(
        repo,
        [
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            "--name-only",
            tree_sha,
            "--",
        ],
        stdout_limit=MAX_GIT_TREE_BYTES,
    )
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        raise GitError(f"git could not read the selected commit tree: {detail}")
    if result.stderr:
        raise GitError("git returned unexpected error output while reading the commit tree")
    return _parse_tree_paths(result.stdout)


def _parse_tree_paths(output: bytes) -> tuple[str, ...]:
    if not output:
        return ()
    if not output.endswith(b"\0"):
        raise GitError("git returned an incomplete tree-path record")
    raw_paths = output[:-1].split(b"\0")
    if len(raw_paths) > MAX_TREE_PATHS:
        raise GitError(f"tree paths exceed the hard {MAX_TREE_PATHS}-path limit")

    paths: set[str] = set()
    for raw_path in raw_paths:
        if not raw_path:
            raise GitError("git returned an empty tree path")
        if (
            len(raw_path) > MAX_TREE_PATH_BYTES
            or raw_path.count(b"/") + 1 > MAX_TREE_PATH_COMPONENTS
        ):
            raise GitError("git returned a non-canonical or unsafe repository-relative path")
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise GitError("git tree path is not valid UTF-8") from error
        _validate_tree_path(path)
        if path in paths:
            raise GitError("git returned a duplicate tree path")
        paths.add(path)
    return tuple(sorted(paths))


def _validate_tree_path(path: str) -> None:
    try:
        _validate_model_tree_path(path)
    except ValueError as error:
        raise GitError("git returned a non-canonical or unsafe repository-relative path") from error
