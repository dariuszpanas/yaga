"""Bounded, shell-free Git commit selection."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from yaga.commits.models import CommitTarget
from yaga.errors import GitError
from yaga.git import (
    GitRepository,
    open_repository,
    parse_object_id,
    require_complete_history,
    run_git,
    safe_git_error,
)

MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_GIT_IDENTITY_BYTES = 256
MAX_REVISION_CHARS = 512
_RANGE_PATTERN = re.compile(r"^(.+?)(\.\.\.|\.\.)(.+)$")
_SET_OPERATOR_PATTERN = re.compile(r"\^(?:@|!|-(?:[0-9]+)?)")


def read_commit(repository: Path, revision: str = "HEAD") -> CommitTarget:
    """Resolve one commit-ish and read that commit's complete message."""
    repo = open_repository(repository)
    resolved = _resolve_commitish(repo, revision, source="--commit")
    _require_complete_commit_history(repo)
    commits = _read_log(
        repo,
        resolved,
        max_commits=1,
        reverse=False,
        detect_overflow=False,
        no_walk=True,
    )
    _require_complete_commit_history(repo)
    if not commits:
        raise GitError(f"revision {_safe_label(revision)!r} did not select a commit")
    return commits[0]


def read_range(repository: Path, revision_range: str, *, max_commits: int) -> list[CommitTarget]:
    """Resolve an explicit range and read its commits in oldest-first order."""
    if max_commits < 1:
        raise GitError("maximum commit count must be positive")
    base, separator, head = _split_explicit_range(revision_range)
    repo = open_repository(repository)
    _require_complete_range_history(repo)
    resolved_base = _resolve_commitish(repo, base, source="--range base")
    resolved_head = _resolve_commitish(repo, head, source="--range head")
    selection = f"{resolved_base}{separator}{resolved_head}"
    commits = _read_log(
        repo,
        selection,
        max_commits=max_commits,
        reverse=True,
        detect_overflow=True,
        no_walk=False,
    )
    _require_complete_range_history(repo)
    if not commits:
        raise GitError(f"revision range {_safe_label(revision_range)!r} selected no commits")
    return commits


def _require_complete_range_history(repo: GitRepository) -> None:
    require_complete_history(
        repo,
        shallow_message="--range requires a non-shallow repository with complete history",
        grafts_message="--range requires complete history without legacy graft overlays",
    )


def _require_complete_commit_history(repo: GitRepository) -> None:
    require_complete_history(
        repo,
        shallow_message="commit selection requires a non-shallow repository with complete history",
        grafts_message="commit selection requires complete history without legacy graft overlays",
    )


def _resolve_commitish(repo: GitRepository, revision: str, *, source: str) -> str:
    _validate_commitish(revision, source=source)
    result = run_git(
        repo,
        [
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ],
        stdout_limit=MAX_GIT_IDENTITY_BYTES,
    )
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        raise GitError(f"git could not resolve {_safe_label(revision)!r} as one commit: {detail}")
    return parse_object_id(result.stdout, label="commit")


def _read_log(
    repo: GitRepository,
    selection: str,
    *,
    max_commits: int,
    reverse: bool,
    detect_overflow: bool,
    no_walk: bool,
) -> list[CommitTarget]:
    command = [
        "log",
        "--no-show-signature",
        "--encoding=UTF-8",
        "--format=%H%x00%P%x00%B",
        "-z",
        f"--max-count={max_commits + int(detect_overflow)}",
    ]
    if reverse:
        command.append("--reverse")
    if no_walk:
        command.append("--no-walk")
    command.extend([selection, "--"])
    result = run_git(repo, command, stdout_limit=MAX_GIT_OUTPUT_BYTES)
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        raise GitError(f"git could not read the resolved commit selection: {detail}")

    commits = _parse_records(result.stdout)
    if detect_overflow and len(commits) > max_commits:
        raise GitError(f"commit selection exceeds the configured limit of {max_commits}")
    return commits


def _parse_records(output: bytes) -> list[CommitTarget]:
    fields = output.split(b"\0")
    while fields and fields[-1] == b"":
        fields.pop()
    if not fields:
        return []
    if len(fields) % 3:
        raise GitError("git returned an incomplete commit record")
    commits: list[CommitTarget] = []
    for index in range(0, len(fields), 3):
        sha_bytes = fields[index]
        sha = parse_object_id(sha_bytes, label="commit")
        parents = _parse_parent_ids(fields[index + 1], object_id_length=len(sha_bytes))
        try:
            message = fields[index + 2].decode("utf-8")
        except UnicodeDecodeError as error:
            raise GitError("git commit message is not valid UTF-8") from error
        commits.append(
            CommitTarget(
                label=f"commit {sha[:12]}",
                sha=sha,
                parents=parents,
                message=message,
            )
        )
    return commits


def _parse_parent_ids(value: bytes, *, object_id_length: int) -> tuple[str, ...]:
    if not value:
        return ()
    raw_parents = value.split(b" ")
    parents = tuple(parse_object_id(parent, label="parent") for parent in raw_parents)
    if any(len(parent) != object_id_length for parent in raw_parents):
        raise GitError("git returned a malformed parent identity")
    return parents


def _split_explicit_range(revision_range: str) -> tuple[str, str, str]:
    _validate_revision(revision_range)
    match = _RANGE_PATTERN.fullmatch(revision_range)
    if match is None:
        raise GitError("--range requires explicit non-empty base and head revisions")
    base, separator, head = match.groups()
    if base.endswith(".") or head.startswith(".") or ".." in base or ".." in head:
        raise GitError("--range requires exactly one two-dot or three-dot separator")
    return base, separator, head


def _validate_commitish(revision: str, *, source: str) -> None:
    _validate_revision(revision)
    if (
        ":" in revision
        or ".." in revision
        or revision.startswith("^")
        or _SET_OPERATOR_PATTERN.search(revision) is not None
    ):
        raise GitError(f"{source} requires exactly one commit-ish, not a path or revision set")


def _validate_revision(revision: str) -> None:
    if (
        not revision
        or len(revision) > MAX_REVISION_CHARS
        or revision.startswith("-")
        or any(
            char.isspace() or unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in revision
        )
    ):
        raise GitError("revision must be one bounded, non-option Git revision expression")


def _safe_label(value: str) -> str:
    return "".join(
        "?" if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char for char in value[:1000]
    )
