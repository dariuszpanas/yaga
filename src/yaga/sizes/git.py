"""Bounded, shell-free blob sizes from one exact committed Git tree."""

from __future__ import annotations

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
from yaga.sizes.models import (
    MAX_SIZE_BYTES,
    MAX_SIZE_ENTRIES,
    MAX_SIZE_PATH_BYTES,
    MAX_SIZE_PATH_COMPONENTS,
    MAX_SIZE_REVISION_CHARS,
    BlobEntry,
    SizeSelection,
    _is_object_id,
    _validate_size_path,
    _validate_size_revision,
)

MAX_GIT_SIZE_BYTES = 64 * 1024 * 1024
_MAX_GIT_IDENTITY_BYTES = 256
_GRAFTS_MESSAGE = "committed blob-size selection rejects legacy graft overlays"
_BLOB_MODES = frozenset({"100644", "100755", "120000"})
_GITLINK_MODE = "160000"
_MAX_SIZE_DECIMAL_DIGITS = len(str(MAX_SIZE_BYTES))


def read_blob_sizes(repository: Path, revision: str) -> SizeSelection:
    """Resolve one commit and return sizes for every blob in its exact tree."""
    _validate_revision(revision)
    repo = open_repository(repository)
    require_no_legacy_grafts(repo, message=_GRAFTS_MESSAGE)
    commit_sha = _resolve_object(repo, revision, object_type="commit")
    tree_sha = _resolve_object(repo, commit_sha, object_type="tree")
    if len(commit_sha) != len(tree_sha):
        raise GitError("git returned inconsistent object identity lengths")
    blobs, gitlinks = _read_blob_size_output(repo, tree_sha, identity_length=len(tree_sha))
    require_no_legacy_grafts(repo, message=_GRAFTS_MESSAGE)
    try:
        return SizeSelection(
            repository=repo.directory,
            revision=revision,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            blobs=blobs,
            gitlinks=gitlinks,
        )
    except ValueError as error:
        raise GitError("git returned an inconsistent committed-tree blob selection") from error


def _validate_revision(revision: str) -> None:
    try:
        _validate_size_revision(revision)
    except ValueError as error:
        raise InputError(
            "size revision must be one bounded, non-option commit-ish "
            f"of at most {MAX_SIZE_REVISION_CHARS} characters"
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


def _read_blob_size_output(
    repo: GitRepository,
    tree_sha: str,
    *,
    identity_length: int,
) -> tuple[tuple[BlobEntry, ...], tuple[str, ...]]:
    result = run_git(
        repo,
        [
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            "--format=%(objectmode)%x20%(objecttype)%x20%(objectname)%x20%(objectsize)%x09%(path)",
            tree_sha,
            "--",
        ],
        stdout_limit=MAX_GIT_SIZE_BYTES,
    )
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        raise GitError(f"git could not read blob sizes from the selected commit tree: {detail}")
    if result.stderr:
        raise GitError("git returned unexpected error output while reading blob sizes")
    return _parse_blob_sizes(result.stdout, identity_length=identity_length)


def _parse_blob_sizes(
    output: bytes,
    *,
    identity_length: int,
) -> tuple[tuple[BlobEntry, ...], tuple[str, ...]]:
    if identity_length not in {40, 64}:
        raise GitError("git returned an unsupported object identity length")
    if not output:
        return (), ()
    if not output.endswith(b"\0"):
        raise GitError("git returned an incomplete blob-size record")
    raw_records = output[:-1].split(b"\0")
    if len(raw_records) > MAX_SIZE_ENTRIES:
        raise GitError(f"tree entries exceed the hard {MAX_SIZE_ENTRIES}-entry limit")

    blobs: list[BlobEntry] = []
    gitlinks: list[str] = []
    paths: set[str] = set()
    for raw_record in raw_records:
        if not raw_record:
            raise GitError("git returned an empty blob-size record")
        raw_metadata, separator, raw_path = raw_record.partition(b"\t")
        if not separator or not raw_path:
            raise GitError("git returned a malformed blob-size record")
        raw_fields = raw_metadata.split(b" ")
        if len(raw_fields) != 4 or any(not field for field in raw_fields):
            raise GitError("git returned malformed blob-size metadata")
        raw_mode, raw_type, raw_oid, raw_size = raw_fields
        try:
            mode = raw_mode.decode("ascii", errors="strict")
            object_type = raw_type.decode("ascii", errors="strict")
            oid = raw_oid.decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise GitError("git returned non-ASCII blob-size metadata") from error

        if len(oid) != identity_length or not _is_object_id(oid):
            raise GitError("git returned a malformed or mixed-format tree-entry identity")
        if (
            len(raw_path) > MAX_SIZE_PATH_BYTES
            or raw_path.count(b"/") + 1 > MAX_SIZE_PATH_COMPONENTS
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

        if mode in _BLOB_MODES:
            if object_type != "blob":
                raise GitError("git returned an incompatible tree-entry mode and type")
            size = _parse_canonical_size(raw_size, allow_unavailable=False)
            assert size is not None
            blobs.append(BlobEntry(path=path, oid=oid, mode=mode, size=size))
        elif mode == _GITLINK_MODE:
            if object_type != "commit":
                raise GitError("git returned an incompatible tree-entry mode and type")
            _parse_canonical_size(raw_size, allow_unavailable=True)
            gitlinks.append(path)
        else:
            raise GitError("git returned an unsupported tree-entry mode")

    return (
        tuple(sorted(blobs, key=lambda entry: entry.path)),
        tuple(sorted(gitlinks)),
    )


def _parse_canonical_size(raw_size: bytes, *, allow_unavailable: bool) -> int | None:
    if allow_unavailable and raw_size == b"-":
        return None
    if raw_size == b"0":
        return 0
    if (
        not raw_size
        or len(raw_size) > _MAX_SIZE_DECIMAL_DIGITS
        or raw_size[0] not in b"123456789"
        or any(byte not in b"0123456789" for byte in raw_size[1:])
    ):
        raise GitError("git returned a non-canonical tree-entry size")
    size = int(raw_size)
    if size > MAX_SIZE_BYTES:
        raise GitError(f"git returned a tree-entry size above the hard {MAX_SIZE_BYTES}-byte limit")
    return size


def _validate_path(path: str) -> None:
    try:
        _validate_size_path(path)
    except ValueError as error:
        raise GitError("git returned a non-canonical or unsafe repository-relative path") from error
