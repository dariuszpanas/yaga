"""Bounded, shell-free selection of paths from one committed Git tree."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from yaga.errors import GitError, InputError, safe_error_text
from yaga.files import read_file_prefix
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
MAX_GIT_ERROR_BYTES = 64 * 1024
MAX_GIT_SECONDS = 30.0

_MAX_GIT_IDENTITY_BYTES = 256
_MAX_GIT_ALTERNATE_DIRECTORIES = 128
_MAX_GIT_METADATA_BYTES = 32 * 1024
_STREAM_CHUNK_BYTES = 64 * 1024
_OBJECT_ID_LENGTHS = frozenset({40, 64})
_LOWER_HEX = frozenset(b"0123456789abcdef")
_PROCESS_ENVIRONMENT_ALLOWLIST = frozenset({"SYSTEMROOT", "WINDIR"})


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_overflow: bool
    stderr_overflow: bool
    timed_out: bool


def read_tree_paths(repository: Path, revision: str) -> TreeSelection:
    """Resolve exactly one commit and return the leaf paths in its committed tree."""
    _validate_revision(revision)
    repo, git = _repository_and_git(repository)
    commit_sha = _resolve_object(repo, git, revision, object_type="commit")
    tree_sha = _resolve_object(repo, git, commit_sha, object_type="tree")
    if len(commit_sha) != len(tree_sha):
        raise GitError("git returned inconsistent object identity lengths")
    paths = _read_tree_path_output(repo, git, tree_sha)
    return TreeSelection(
        repository=repo,
        revision=revision,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
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


def _repository_and_git(repository: Path) -> tuple[Path, str]:
    if not isinstance(repository, Path):
        raise InputError("repository must be a filesystem path")
    try:
        repo = repository.expanduser().resolve(strict=True)
        metadata = repo.stat()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(repository, maximum=300)
        raise InputError(f"repository path cannot be resolved safely: {display}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        display = safe_error_text(repo, maximum=300)
        raise InputError(f"repository directory does not exist: {display}")

    trust_boundaries = _repository_trust_boundaries(repo)

    discovered_git = shutil.which("git")
    if discovered_git is None:
        raise GitError("git executable was not found")
    git_path = Path(discovered_git)
    if not git_path.is_absolute():
        raise GitError("git executable path must be absolute")
    if any(_is_within(git_path, boundary) for boundary in trust_boundaries):
        raise GitError("git executable must not be discovered inside the repository")
    try:
        resolved_git = git_path.resolve(strict=True)
        git_metadata = resolved_git.stat()
    except (OSError, RuntimeError, ValueError) as error:
        raise GitError("git executable path cannot be resolved safely") from error
    if not stat.S_ISREG(git_metadata.st_mode):
        raise GitError("git executable is not a regular file")
    if any(_is_within(resolved_git, boundary) for boundary in trust_boundaries):
        raise GitError("git executable must not resolve inside the repository")
    return repo, str(resolved_git)


def _repository_trust_boundaries(directory: Path) -> tuple[Path, ...]:
    """Return enclosing worktree and resolved Git-metadata trust boundaries."""
    boundaries: list[Path] = []
    for candidate in (directory, *directory.parents):
        marker = candidate / ".git"
        if _path_entry_exists(marker):
            boundaries.append(candidate)
            boundaries.extend(_git_marker_boundaries(marker))
        if _looks_like_bare_repository(candidate):
            boundaries.append(candidate)
            boundaries.extend(_git_directory_boundaries(candidate))
    if not boundaries:
        return (directory,)
    return tuple(dict.fromkeys(boundaries))


def _looks_like_bare_repository(directory: Path) -> bool:
    return all(_path_entry_exists(directory / name) for name in ("HEAD", "objects"))


def _git_marker_boundaries(marker: Path) -> tuple[Path, ...]:
    try:
        resolved_marker = marker.resolve(strict=True)
        metadata = resolved_marker.stat()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(marker, maximum=300)
        raise InputError(f"repository .git marker cannot be resolved safely: {display}") from error
    if stat.S_ISDIR(metadata.st_mode):
        return _git_directory_boundaries(resolved_marker)
    if not stat.S_ISREG(metadata.st_mode):
        raise InputError("repository .git marker must resolve to a file or directory")
    git_directory = _read_metadata_directory(
        marker,
        prefix="gitdir: ",
        relative_to=marker.parent,
        label="repository .git file",
    )
    return _git_directory_boundaries(git_directory)


def _git_directory_boundaries(git_directory: Path) -> tuple[Path, ...]:
    common_marker = git_directory / "commondir"
    common_directory = git_directory
    if _path_entry_exists(common_marker):
        common_directory = _read_metadata_directory(
            common_marker,
            prefix=None,
            relative_to=git_directory,
            label="repository commondir file",
        )
    object_boundaries = _git_object_boundaries(common_directory / "objects")
    return tuple(dict.fromkeys((git_directory, common_directory, *object_boundaries)))


def _git_object_boundaries(initial_directory: Path) -> tuple[Path, ...]:
    pending = [initial_directory]
    boundaries: list[Path] = []
    seen: set[Path] = set()
    while pending:
        directory = _resolve_metadata_directory(
            pending.pop(),
            label="repository object directory",
        )
        if directory in seen:
            continue
        if len(seen) >= _MAX_GIT_ALTERNATE_DIRECTORIES:
            raise InputError(
                "repository object alternates exceed the hard "
                f"{_MAX_GIT_ALTERNATE_DIRECTORIES}-directory limit"
            )
        seen.add(directory)
        boundaries.append(directory)
        alternates = directory / "info" / "alternates"
        if _path_entry_exists(alternates):
            pending.extend(_read_alternate_directories(alternates, relative_to=directory))
    return tuple(boundaries)


def _read_alternate_directories(path: Path, *, relative_to: Path) -> tuple[Path, ...]:
    try:
        raw = read_file_prefix(path, maximum=_MAX_GIT_METADATA_BYTES)
    except OSError as error:
        display = safe_error_text(path, maximum=300)
        raise InputError(
            f"repository object alternates cannot be read safely: {display}"
        ) from error
    if len(raw) > _MAX_GIT_METADATA_BYTES:
        raise InputError(f"repository object alternates exceed {_MAX_GIT_METADATA_BYTES} bytes")
    value = os.fsdecode(raw)
    if "\0" in value:
        raise InputError("repository object alternates contain an unsafe path")
    lines = value.splitlines()
    if any(not line for line in lines):
        raise InputError("repository object alternates must contain one path per line")
    if len(lines) > _MAX_GIT_ALTERNATE_DIRECTORIES:
        raise InputError(
            "repository object alternates exceed the hard "
            f"{_MAX_GIT_ALTERNATE_DIRECTORIES}-directory limit"
        )
    directories = []
    for line in lines:
        selected = Path(line)
        if not selected.is_absolute():
            selected = relative_to / selected
        directories.append(selected)
    return tuple(directories)


def _read_metadata_directory(
    path: Path,
    *,
    prefix: str | None,
    relative_to: Path,
    label: str,
) -> Path:
    try:
        raw = read_file_prefix(path, maximum=_MAX_GIT_METADATA_BYTES)
    except OSError as error:
        display = safe_error_text(path, maximum=300)
        raise InputError(f"{label} cannot be read safely: {display}") from error
    if len(raw) > _MAX_GIT_METADATA_BYTES:
        raise InputError(f"{label} exceeds {_MAX_GIT_METADATA_BYTES} bytes")
    value = os.fsdecode(raw)
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    if not value or "\0" in value or "\r" in value or "\n" in value:
        raise InputError(f"{label} must contain one bounded path")
    if prefix is not None:
        if not value.startswith(prefix) or len(value) == len(prefix):
            raise InputError(f"{label} must contain one {prefix.strip()!r} path")
        value = value[len(prefix) :]
    selected = Path(value)
    if not selected.is_absolute():
        selected = relative_to / selected
    return _resolve_metadata_directory(selected, label=label)


def _resolve_metadata_directory(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(path, maximum=300)
        raise InputError(f"{label} cannot be resolved safely: {display}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise InputError(f"{label} must resolve to a directory")
    return resolved


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        display = safe_error_text(path, maximum=300)
        raise InputError(f"repository boundary cannot be inspected safely: {display}") from error
    return True


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _resolve_object(
    repo: Path,
    git: str,
    revision: str,
    *,
    object_type: str,
) -> str:
    if object_type not in {"commit", "tree"}:
        raise ValueError("object type must be commit or tree")
    result = _run_git(
        [
            git,
            "--no-lazy-fetch",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{{object_type}}}",
        ],
        stdout_limit=_MAX_GIT_IDENTITY_BYTES,
    )
    if result.returncode != 0:
        detail = _safe_git_error(result.stderr)
        if object_type == "commit":
            label = safe_error_text(revision, maximum=160)
            raise GitError(f"git could not resolve {label!r} as one commit: {detail}")
        raise GitError(f"git could not resolve the selected commit tree: {detail}")
    return _parse_single_identity(result.stdout, label=object_type)


def _parse_single_identity(output: bytes, *, label: str) -> str:
    lines = output.splitlines()
    if len(lines) != 1 or not _is_object_id(lines[0]):
        raise GitError(f"git returned a malformed {label} identity")
    return lines[0].decode("ascii")


def _is_object_id(value: bytes) -> bool:
    return len(value) in _OBJECT_ID_LENGTHS and all(byte in _LOWER_HEX for byte in value)


def _read_tree_path_output(repo: Path, git: str, tree_sha: str) -> tuple[str, ...]:
    result = _run_git(
        [
            git,
            "--no-lazy-fetch",
            "-C",
            str(repo),
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
        detail = _safe_git_error(result.stderr)
        raise GitError(f"git could not read the selected commit tree: {detail}")
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


def _run_git(command: list[str], *, stdout_limit: int) -> _ProcessResult:
    try:
        result = _run_bounded(
            command,
            stdout_limit=stdout_limit,
            stderr_limit=MAX_GIT_ERROR_BYTES,
            timeout_seconds=MAX_GIT_SECONDS,
        )
    except OSError as error:
        detail = safe_error_text(error, maximum=300)
        raise GitError(f"could not execute git: {detail}") from error
    if result.timed_out:
        raise GitError(f"git command exceeded the hard {MAX_GIT_SECONDS:g}-second limit")
    if result.stdout_overflow:
        raise GitError(f"git output exceeds the hard {stdout_limit}-byte limit")
    if result.stderr_overflow:
        raise GitError(f"git error output exceeds the hard {MAX_GIT_ERROR_BYTES}-byte limit")
    return result


def _run_bounded(
    command: list[str],
    *,
    stdout_limit: int,
    stderr_limit: int,
    timeout_seconds: float = MAX_GIT_SECONDS,
) -> _ProcessResult:
    """Run one process while retaining at most the configured pipe bytes."""
    if stdout_limit < 1 or stderr_limit < 1 or timeout_seconds <= 0:
        raise ValueError("process output limits must be positive")
    process = subprocess.Popen(  # noqa: S603 - resolved executable, fixed arguments, no shell
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
        shell=False,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    stdout = bytearray()
    stderr = bytearray()
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    reader_errors: list[OSError] = []
    kill_lock = threading.Lock()

    def kill_process() -> None:
        with kill_lock:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass

    threads = [
        threading.Thread(
            target=_capture_stream,
            args=(
                process.stdout,
                stdout_limit,
                stdout,
                stdout_overflow,
                kill_process,
                reader_errors,
            ),
        ),
        threading.Thread(
            target=_capture_stream,
            args=(
                process.stderr,
                stderr_limit,
                stderr,
                stderr_overflow,
                kill_process,
                reader_errors,
            ),
        ),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process()
        returncode = process.wait()
    for thread in threads:
        thread.join()
    if reader_errors:
        raise OSError("could not read process output") from reader_errors[0]
    return _ProcessResult(
        returncode=returncode,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
        stdout_overflow=stdout_overflow.is_set(),
        stderr_overflow=stderr_overflow.is_set(),
        timed_out=timed_out,
    )


def _capture_stream(
    stream: IO[bytes],
    limit: int,
    destination: bytearray,
    overflow: threading.Event,
    kill_process: Callable[[], None],
    reader_errors: list[OSError],
) -> None:
    try:
        while True:
            remaining = limit - len(destination)
            chunk = stream.read(min(_STREAM_CHUNK_BYTES, remaining + 1))
            if not chunk:
                return
            if len(chunk) > remaining:
                destination.extend(chunk[:remaining])
                overflow.set()
                kill_process()
                return
            destination.extend(chunk)
    except OSError as error:
        reader_errors.append(error)
        kill_process()
    finally:
        stream.close()


def _git_environment() -> dict[str, str]:
    environment = {
        name: os.environ[name] for name in _PROCESS_ENVIRONMENT_ALLOWLIST if name in os.environ
    }
    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _safe_git_error(value: bytes) -> str:
    decoded = value[:1000].decode("utf-8", errors="replace")
    return safe_error_text(decoded, maximum=1000) or "unknown git error"
