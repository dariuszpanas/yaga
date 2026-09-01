"""Shared exact identity for one committed Git tree."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from yaga.errors import GitError, InputError, safe_error_text
from yaga.git.runtime import (
    GitRepository,
    open_repository,
    parse_object_id,
    require_no_legacy_grafts,
    run_git,
    safe_git_error,
)

MAX_COMMITTED_TREE_REVISION_CHARS = 512

_DEFAULT_GRAFTS_MESSAGE = "committed-tree identity rejects legacy graft overlays"
_IDENTITY_BYTES = 256
_IDENTITY_PROOF = object()
_OBJECT_ID_LENGTHS = frozenset({40, 64})
_LOWER_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class CommittedTreeIdentity:
    """One resolver-created repository, commit, and tree identity.

    The original revision spelling is retained for provider reports, while all
    subsequent enumeration is bound to ``tree_sha``.  Instances are created by
    :func:`resolve_committed_tree`; callers cannot supply an unchecked identity.
    """

    repository: GitRepository
    revision: str
    commit_sha: str
    tree_sha: str
    _proof: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _IDENTITY_PROOF:
            raise ValueError("committed-tree identity must come from the shared resolver")
        if type(self.repository) is not GitRepository:
            raise ValueError("committed-tree identity requires a trusted Git repository")
        if (
            not self.repository.directory.is_absolute()
            or not Path(self.repository.executable).is_absolute()
        ):
            raise ValueError("committed-tree identity requires absolute repository resources")
        _validate_revision(self.revision)
        if (
            not _is_object_id(self.commit_sha)
            or not _is_object_id(self.tree_sha)
            or len(self.commit_sha) != len(self.tree_sha)
        ):
            raise ValueError("committed-tree identity requires same-format lowercase object IDs")


def resolve_committed_tree(
    repository: Path,
    revision: str,
    *,
    grafts_message: str = _DEFAULT_GRAFTS_MESSAGE,
) -> CommittedTreeIdentity:
    """Resolve one explicit commit-ish to an exact commit and tree once."""
    _validate_revision_input(revision)
    if not isinstance(grafts_message, str) or not grafts_message:
        raise ValueError("grafts message must be a nonempty string")

    selected = open_repository(repository)
    require_no_legacy_grafts(selected, message=grafts_message)
    commit_sha = _resolve_object(selected, revision, object_type="commit")
    tree_sha = _resolve_object(selected, commit_sha, object_type="tree")
    if len(commit_sha) != len(tree_sha):
        raise GitError("git returned inconsistent object identity lengths")
    require_no_legacy_grafts(selected, message=grafts_message)
    return CommittedTreeIdentity(
        repository=selected,
        revision=revision,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        _proof=_IDENTITY_PROOF,
    )


def require_matching_identity(
    identity: CommittedTreeIdentity,
    *,
    repository: Path,
    revision: str,
) -> CommittedTreeIdentity:
    """Require a resolver-created identity for the exact requested source."""
    if type(identity) is not CommittedTreeIdentity or identity._proof is not _IDENTITY_PROOF:
        raise InputError("committed-tree identity is not trusted")
    _validate_revision_input(revision)
    if revision != identity.revision:
        raise InputError("committed-tree identity does not match the requested revision")
    directory = _resolve_repository_directory(repository)
    if directory != identity.repository.directory:
        raise InputError("committed-tree identity does not match the requested repository")
    return identity


def _resolve_object(
    repository: GitRepository,
    revision: str,
    *,
    object_type: str,
) -> str:
    if object_type not in {"commit", "tree"}:
        raise ValueError("object type must be commit or tree")
    result = run_git(
        repository,
        [
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{{object_type}}}",
        ],
        stdout_limit=_IDENTITY_BYTES,
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


def _validate_revision_input(revision: object) -> str:
    try:
        return _validate_revision(revision)
    except ValueError as error:
        raise InputError(
            "revision must be one bounded, non-option commit-ish "
            f"of at most {MAX_COMMITTED_TREE_REVISION_CHARS} characters"
        ) from error


def _validate_revision(revision: object) -> str:
    if (
        not isinstance(revision, str)
        or not revision
        or len(revision) > MAX_COMMITTED_TREE_REVISION_CHARS
        or revision.startswith(("-", "^"))
        or ":" in revision
        or ".." in revision
        or any(selector in revision for selector in ("^@", "^!", "^-"))
        or any(
            character.isspace() or unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in revision
        )
    ):
        raise ValueError("revision must be one bounded, non-option commit-ish")
    return revision


def _resolve_repository_directory(repository: object) -> Path:
    if not isinstance(repository, Path):
        raise InputError("repository must be a filesystem path")
    try:
        directory = repository.expanduser().resolve(strict=True)
        if not directory.is_dir():
            raise OSError("not a directory")
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(repository, maximum=300)
        raise InputError(f"repository path cannot be resolved safely: {display}") from error
    return directory


def _is_object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in _OBJECT_ID_LENGTHS
        and all(character in _LOWER_HEX for character in value)
    )
