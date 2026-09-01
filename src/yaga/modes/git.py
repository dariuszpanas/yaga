"""Bounded, shell-free modes from one exact committed Git tree."""

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
from yaga.modes.models import (
    MAX_MODE_ENTRIES,
    MAX_MODE_PATH_BYTES,
    MAX_MODE_PATH_COMPONENTS,
    MAX_MODE_REVISION_CHARS,
    ModeEntry,
    ModeSelection,
    _is_object_id,
    _validate_mode_path,
    _validate_mode_revision,
)

MAX_GIT_MODE_BYTES = 64 * 1024 * 1024
_GRAFTS_MESSAGE = "committed-mode selection rejects legacy graft overlays"
_MODE_TYPES = {
    "100644": "blob",
    "100755": "blob",
    "120000": "blob",
    "160000": "commit",
}


def read_mode_selection(
    repository: Path,
    revision: str,
    *,
    identity: CommittedTreeIdentity | None = None,
) -> ModeSelection:
    """Resolve one commit and return every leaf mode from its exact tree."""
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
    entries = _read_mode_output(
        repo,
        identity.tree_sha,
        identity_length=len(identity.tree_sha),
    )
    require_no_legacy_grafts(repo, message=_GRAFTS_MESSAGE)
    try:
        return ModeSelection(
            repository=repo.directory,
            revision=identity.revision,
            commit_sha=identity.commit_sha,
            tree_sha=identity.tree_sha,
            entries=entries,
        )
    except ValueError as error:
        raise GitError("git returned an inconsistent committed-mode selection") from error


def _validate_revision(revision: str) -> None:
    try:
        _validate_mode_revision(revision)
    except ValueError as error:
        raise InputError(
            "mode revision must be one bounded, non-option commit-ish "
            f"of at most {MAX_MODE_REVISION_CHARS} characters"
        ) from error


def _read_mode_output(
    repo: GitRepository,
    tree_sha: str,
    *,
    identity_length: int,
) -> tuple[ModeEntry, ...]:
    result = run_git(
        repo,
        [
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            "--format=%(objectmode)%x20%(objecttype)%x20%(objectname)%x09%(path)",
            tree_sha,
            "--",
        ],
        stdout_limit=MAX_GIT_MODE_BYTES,
    )
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        raise GitError(f"git could not read modes from the selected commit tree: {detail}")
    if result.stderr:
        raise GitError("git returned unexpected error output while reading committed modes")
    return _parse_mode_entries(result.stdout, identity_length=identity_length)


def _parse_mode_entries(
    output: bytes,
    *,
    identity_length: int,
) -> tuple[ModeEntry, ...]:
    if identity_length not in {40, 64}:
        raise GitError("git returned an unsupported object identity length")
    if not output:
        return ()
    if not output.endswith(b"\0"):
        raise GitError("git returned an incomplete committed-mode record")
    raw_records = output[:-1].split(b"\0")
    if len(raw_records) > MAX_MODE_ENTRIES:
        raise GitError(f"tree entries exceed the hard {MAX_MODE_ENTRIES}-entry limit")

    entries: list[ModeEntry] = []
    paths: set[str] = set()
    for raw_record in raw_records:
        if not raw_record:
            raise GitError("git returned an empty committed-mode record")
        raw_metadata, separator, raw_path = raw_record.partition(b"\t")
        if not separator or not raw_path:
            raise GitError("git returned a malformed committed-mode record")
        raw_fields = raw_metadata.split(b" ")
        if len(raw_fields) != 3 or any(not field for field in raw_fields):
            raise GitError("git returned malformed committed-mode metadata")
        raw_mode, raw_type, raw_oid = raw_fields
        try:
            mode = raw_mode.decode("ascii", errors="strict")
            object_type = raw_type.decode("ascii", errors="strict")
            oid = raw_oid.decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise GitError("git returned non-ASCII committed-mode metadata") from error

        expected_type = _MODE_TYPES.get(mode)
        if expected_type is None:
            raise GitError("git returned an unsupported tree-entry mode")
        if object_type != expected_type:
            raise GitError("git returned an incompatible tree-entry mode and type")
        if len(oid) != identity_length or not _is_object_id(oid):
            raise GitError("git returned a malformed or mixed-format tree-entry identity")
        if (
            len(raw_path) > MAX_MODE_PATH_BYTES
            or raw_path.count(b"/") + 1 > MAX_MODE_PATH_COMPONENTS
        ):
            raise GitError("git returned a non-canonical or unsafe repository-relative path")
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise GitError("git tree path is not valid UTF-8") from error
        _validate_path(path)
        if path in paths:
            raise GitError("git returned a duplicate tree path")
        paths.add(path)

        try:
            entries.append(ModeEntry(path=path, oid=oid, mode=mode, object_type=object_type))
        except ValueError as error:
            raise GitError("git returned inconsistent committed-mode metadata") from error

    return tuple(sorted(entries, key=lambda entry: entry.path))


def _validate_path(path: str) -> None:
    try:
        _validate_mode_path(path)
    except ValueError as error:
        raise GitError("git returned a non-canonical or unsafe repository-relative path") from error
