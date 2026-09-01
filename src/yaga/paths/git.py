"""Bounded, shell-free path selection from one exact committed Git tree."""

from __future__ import annotations

from bisect import bisect_left
from pathlib import Path

from yaga.errors import GitError, InputError, safe_error_text
from yaga.git import (
    GitRepository,
    open_repository,
    parse_object_id,
    require_no_legacy_grafts,
    run_git,
    safe_git_error,
)
from yaga.paths.models import (
    MAX_PATH_BYTES,
    MAX_PATH_COMPONENTS,
    MAX_PATH_ENTRIES,
    MAX_PATH_REVISION_CHARS,
    PathSelection,
    _validate_committed_path,
    _validate_path_revision,
)

MAX_GIT_PATH_BYTES = 64 * 1024 * 1024
_MAX_GIT_IDENTITY_BYTES = 256
_GRAFTS_MESSAGE = "committed-path selection rejects legacy graft overlays"


def read_path_selection(repository: Path, revision: str) -> PathSelection:
    """Resolve one commit and return every leaf path from its exact tree."""
    _validate_revision(revision)
    repo = open_repository(repository)
    require_no_legacy_grafts(repo, message=_GRAFTS_MESSAGE)
    commit_sha = _resolve_object(repo, revision, object_type="commit")
    tree_sha = _resolve_object(repo, commit_sha, object_type="tree")
    if len(commit_sha) != len(tree_sha):
        raise GitError("git returned inconsistent object identity lengths")
    paths = _read_path_output(repo, tree_sha)
    require_no_legacy_grafts(repo, message=_GRAFTS_MESSAGE)
    try:
        return PathSelection(
            repository=repo.directory,
            revision=revision,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            paths=paths,
        )
    except ValueError as error:
        raise GitError("git returned an inconsistent committed-path selection") from error


def _validate_revision(revision: str) -> None:
    try:
        _validate_path_revision(revision)
    except ValueError as error:
        raise InputError(
            "path revision must be one bounded, non-option commit-ish "
            f"of at most {MAX_PATH_REVISION_CHARS} characters"
        ) from error


def _resolve_object(
    repo: GitRepository,
    revision: str,
    *,
    object_type: str,
) -> str:
    if object_type not in {"commit", "tree"}:
        raise ValueError("object type must be commit or tree")
    result = run_git(
        repo,
        [
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{{object_type}}}",
        ],
        stdout_limit=_MAX_GIT_IDENTITY_BYTES,
    )
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        if object_type == "commit":
            label = safe_error_text(revision, maximum=160)
            raise GitError(f"git could not resolve {label!r} as one commit: {detail}")
        raise GitError(f"git could not resolve the selected commit tree: {detail}")
    if result.stderr:
        raise GitError(f"git returned unexpected error output while resolving {object_type}")
    return parse_object_id(result.stdout, label=object_type)


def _read_path_output(repo: GitRepository, tree_sha: str) -> tuple[str, ...]:
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
        stdout_limit=MAX_GIT_PATH_BYTES,
    )
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        raise GitError(f"git could not read the selected commit paths: {detail}")
    if result.stderr:
        raise GitError("git returned unexpected error output while reading commit paths")
    return _parse_paths(result.stdout)


def _parse_paths(output: bytes) -> tuple[str, ...]:
    if not output:
        return ()
    if not output.endswith(b"\0"):
        raise GitError("git returned an incomplete committed-path record")
    raw_paths = output[:-1].split(b"\0")
    if len(raw_paths) > MAX_PATH_ENTRIES:
        raise GitError(f"committed paths exceed the hard {MAX_PATH_ENTRIES}-entry limit")

    paths: set[str] = set()
    for raw_path in raw_paths:
        if not raw_path:
            raise GitError("git returned an empty committed path")
        if len(raw_path) > MAX_PATH_BYTES or raw_path.count(b"/") + 1 > MAX_PATH_COMPONENTS:
            raise GitError("git returned an unsafe repository-relative committed path")
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise GitError("git committed path is not valid UTF-8") from error
        try:
            _validate_committed_path(path)
        except ValueError as error:
            raise GitError("git returned an unsafe repository-relative committed path") from error
        if path in paths:
            raise GitError("git returned a duplicate committed path")
        paths.add(path)

    ordered = tuple(sorted(paths))
    _validate_leaf_topology(ordered)
    return ordered


def _validate_leaf_topology(paths: tuple[str, ...]) -> None:
    for path in paths:
        prefix = f"{path}/"
        candidate_index = bisect_left(paths, prefix)
        if candidate_index < len(paths) and paths[candidate_index].startswith(prefix):
            raise GitError("git returned an impossible committed-path leaf topology")
