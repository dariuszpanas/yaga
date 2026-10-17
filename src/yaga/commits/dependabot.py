"""Narrow an event-author exemption using commit metadata and signatures.

Git metadata is forgeable. This filter never establishes bot identity: callers must
first validate the exact Dependabot PR author and opt-in policy. Its only purpose
is to stop additions inheriting that PR's commit-message exemption. By default,
each compatible commit must also verify against YAGA's pinned GitHub signing key.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from pathlib import Path

from yaga.commits.github_signature import verified_github_commits
from yaga.commits.models import CommitTarget
from yaga.errors import GitError
from yaga.git import open_repository, parse_object_id, require_complete_history, run_git

MAX_AUTHOR_FIELD_BYTES = 256
MAX_AUTHOR_OUTPUT_BYTES = 16 * 1024 * 1024
_BOT_NAME = "dependabot[bot]"
_BOT_EMAILS = frozenset(
    {
        "49699333+dependabot[bot]@users.noreply.github.com",
        "dependabot[bot]@users.noreply.github.com",
    }
)


def filter_dependabot_commits(
    repository: Path,
    *,
    base_sha: str,
    head_sha: str,
    targets: Sequence[CommitTarget],
    require_github_signature: bool = True,
) -> frozenset[str]:
    """Retain qualifying commits from an already eligible, complete PR range."""
    base = parse_object_id(base_sha.encode("ascii"), label="pull request base")
    head = parse_object_id(head_sha.encode("ascii"), label="pull request head")
    expected = {target.sha for target in targets}
    if not targets or None in expected or len(expected) != len(targets):
        raise GitError("Dependabot filter requires a complete unique commit selection")
    repo = open_repository(repository)
    require_complete_history(
        repo,
        shallow_message="Dependabot filter requires non-shallow history",
        grafts_message="Dependabot filter requires history without legacy graft overlays",
    )
    result = run_git(
        repo,
        [
            "log",
            "--no-show-signature",
            "--encoding=UTF-8",
            "--format=%H%x00%an%x00%ae%x00%cn%x00%ce",
            "-z",
            f"--max-count={len(targets) + 1}",
            f"{base}..{head}",
            "--",
        ],
        stdout_limit=MAX_AUTHOR_OUTPUT_BYTES,
    )
    require_complete_history(
        repo,
        shallow_message="Dependabot filter requires non-shallow history",
        grafts_message="Dependabot filter requires history without legacy graft overlays",
    )
    if result.returncode:
        raise GitError("git could not read the complete commit metadata selection")
    records = _parse_records(result.stdout)
    if records.keys() != expected:
        raise GitError("commit metadata does not match the complete selected SHA set")
    candidates = tuple(
        sha
        for sha, (author, author_email, committer, committer_email) in records.items()
        if author == _BOT_NAME
        and author_email in _BOT_EMAILS
        and (
            (committer == "GitHub" and committer_email == "noreply@github.com")
            or (committer == _BOT_NAME and committer_email in _BOT_EMAILS)
        )
    )
    if require_github_signature and candidates:
        return verified_github_commits(repo, candidates)
    return frozenset(candidates)


def _parse_records(output: bytes) -> dict[str, tuple[str, str, str, str]]:
    if not output or not output.endswith(b"\0"):
        raise GitError("git returned incomplete commit metadata")
    fields = output[:-1].split(b"\0")
    if len(fields) % 5:
        raise GitError("git returned incomplete commit metadata")
    records: dict[str, tuple[str, str, str, str]] = {}
    for offset in range(0, len(fields), 5):
        sha = parse_object_id(fields[offset], label="commit metadata SHA")
        if sha in records:
            raise GitError("git returned duplicate commit metadata")
        records[sha] = (
            _identity_field(fields[offset + 1]),
            _identity_field(fields[offset + 2]),
            _identity_field(fields[offset + 3]),
            _identity_field(fields[offset + 4]),
        )
    return records


def _identity_field(value: bytes) -> str:
    if len(value) > MAX_AUTHOR_FIELD_BYTES:
        raise GitError("commit metadata identity exceeds the hard byte limit")
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GitError("commit metadata identity is not valid UTF-8") from error
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in decoded):
        raise GitError("commit metadata identity contains unsupported control characters")
    return decoded
