"""Bounded, shell-free Git changed-path selection."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

from yaga.changes.models import (
    MAX_CHANGED_PATH_BYTES,
    MAX_CHANGED_PATH_COMPONENTS,
    MAX_CHANGED_PATHS,
    MAX_REVISION_RANGE_CHARS,
    ChangeSelection,
)
from yaga.errors import GitError, safe_error_text
from yaga.git import (
    GitRepository,
    open_repository,
    parse_object_id,
    require_complete_history,
    run_git,
    safe_git_error,
)

MAX_REVISION_CHARS = MAX_REVISION_RANGE_CHARS
MAX_GIT_DIFF_BYTES = 4 * 1024 * 1024

_MAX_GIT_IDENTITY_BYTES = 256
_MAX_MERGE_BASE_BYTES = 4096
_SHALLOW_HISTORY_MESSAGE = "changed paths require a non-shallow repository with complete history"
_GRAFTS_HISTORY_MESSAGE = "changed paths require complete history without legacy graft overlays"
_RANGE_PATTERN = re.compile(r"^(.+?)(\.\.\.|\.\.)(.+)$")
_SET_OPERATOR_PATTERN = re.compile(r"\^(?:@|!|-(?:[0-9]+)?)")
_UNSAFE_PATH_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def read_changed_paths(repository: Path, revision_range: str) -> ChangeSelection:
    """Resolve an exact range and return its bounded changed repository paths."""
    base, separator, head = _split_explicit_range(revision_range)
    repo = open_repository(repository)
    require_complete_history(
        repo,
        shallow_message=_SHALLOW_HISTORY_MESSAGE,
        grafts_message=_GRAFTS_HISTORY_MESSAGE,
    )
    base_sha = _resolve_commitish(repo, base, source="range base")
    head_sha = _resolve_commitish(repo, head, source="range head")
    if len(base_sha) != len(head_sha):
        raise GitError("git returned inconsistent commit identity lengths")

    comparison_sha = base_sha
    if separator == "...":
        comparison_sha = _resolve_unique_merge_base(repo, base_sha, head_sha)
        if len(comparison_sha) != len(base_sha):
            raise GitError("git returned an inconsistent merge-base identity length")

    paths = _read_changed_path_output(repo, comparison_sha, head_sha)
    require_complete_history(
        repo,
        shallow_message=_SHALLOW_HISTORY_MESSAGE,
        grafts_message=_GRAFTS_HISTORY_MESSAGE,
    )
    return ChangeSelection(
        revision_range=revision_range,
        base_sha=base_sha,
        head_sha=head_sha,
        comparison_sha=comparison_sha,
        paths=paths,
    )


def _split_explicit_range(revision_range: str) -> tuple[str, str, str]:
    _validate_revision(revision_range)
    match = _RANGE_PATTERN.fullmatch(revision_range)
    if match is None:
        raise GitError("changed paths require an explicit non-empty A..B or A...B range")
    base, separator, head = match.groups()
    if base.endswith(".") or head.startswith(".") or ".." in base or ".." in head:
        raise GitError("changed paths require exactly one two-dot or three-dot separator")
    _validate_commitish(base)
    _validate_commitish(head)
    return base, separator, head


def _validate_commitish(revision: str) -> None:
    _validate_revision(revision)
    if (
        ".." in revision
        or revision.startswith("^")
        or _SET_OPERATOR_PATTERN.search(revision) is not None
    ):
        raise GitError("changed-path range endpoints must each name exactly one commit-ish")


def _validate_revision(revision: str) -> None:
    if (
        not isinstance(revision, str)
        or not revision
        or len(revision) > MAX_REVISION_CHARS
        or revision.startswith("-")
        or any(
            character.isspace() or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in revision
        )
    ):
        raise GitError("revision range must be one bounded, non-option Git expression")


def _resolve_commitish(repo: GitRepository, revision: str, *, source: str) -> str:
    result = run_git(
        repo,
        [
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ],
        stdout_limit=_MAX_GIT_IDENTITY_BYTES,
    )
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        label = safe_error_text(revision, maximum=160)
        raise GitError(f"git could not resolve {source} {label!r} as one commit: {detail}")
    return parse_object_id(result.stdout, label="commit")


def _resolve_unique_merge_base(repo: GitRepository, base_sha: str, head_sha: str) -> str:
    result = run_git(
        repo,
        ["merge-base", "--all", base_sha, head_sha],
        stdout_limit=_MAX_MERGE_BASE_BYTES,
    )
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        raise GitError(f"git could not determine one merge base: {detail}")
    lines = result.stdout.splitlines()
    if len(lines) > 1:
        raise GitError("three-dot changed paths require exactly one merge base")
    return parse_object_id(result.stdout, label="merge-base")


def _read_changed_path_output(
    repo: GitRepository,
    comparison_sha: str,
    head_sha: str,
) -> tuple[str, ...]:
    result = run_git(
        repo,
        [
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            comparison_sha,
            head_sha,
            "--",
        ],
        stdout_limit=MAX_GIT_DIFF_BYTES,
    )
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        raise GitError(f"git could not read changed paths: {detail}")
    return _parse_changed_paths(result.stdout)


def _parse_changed_paths(output: bytes) -> tuple[str, ...]:
    if not output:
        return ()
    if not output.endswith(b"\0"):
        raise GitError("git returned an incomplete changed-path record")
    raw_paths = output[:-1].split(b"\0")
    if len(raw_paths) > MAX_CHANGED_PATHS:
        raise GitError(f"changed paths exceed the hard {MAX_CHANGED_PATHS}-path limit")

    paths: set[str] = set()
    for raw_path in raw_paths:
        if not raw_path:
            raise GitError("git returned an empty changed path")
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise GitError("git changed path is not valid UTF-8") from error
        _validate_changed_path(path)
        if path in paths:
            raise GitError("git returned a duplicate changed path")
        paths.add(path)
    return tuple(sorted(paths))


def _validate_changed_path(path: str) -> None:
    posix = PurePosixPath(path)
    try:
        encoded_length = len(path.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:  # pragma: no cover - decoded above
        raise GitError("git changed path is not valid UTF-8") from error
    if (
        not path
        or posix.as_posix() != path
        or posix.is_absolute()
        or "\\" in path
        or bool(PureWindowsPath(path).drive)
        or not posix.parts
        or any(part in {"", ".", ".."} for part in posix.parts)
        or len(posix.parts) > MAX_CHANGED_PATH_COMPONENTS
        or encoded_length > MAX_CHANGED_PATH_BYTES
        or any(unicodedata.category(character) in _UNSAFE_PATH_CATEGORIES for character in path)
    ):
        raise GitError("git returned a non-canonical or unsafe repository-relative path")
