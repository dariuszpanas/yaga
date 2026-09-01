"""Dependency-free, bounded Git repository and process primitives."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from yaga.errors import GitError, InputError, safe_error_text
from yaga.git.process import ProcessTree, open_process_tree

MAX_GIT_ERROR_BYTES = 64 * 1024
MAX_GIT_SECONDS = 30.0

_MAX_GIT_ALTERNATE_DIRECTORIES = 128
_MAX_GIT_COMMON_DIR_BYTES = 32 * 1024
_MAX_GIT_METADATA_BYTES = 32 * 1024
_OBJECT_ID_LENGTHS = frozenset({40, 64})
_LOWER_HEX = frozenset(b"0123456789abcdef")
_PROCESS_ENVIRONMENT_ALLOWLIST = frozenset({"SYSTEMROOT", "WINDIR"})
_PROCESS_CLEANUP_SECONDS = 1.0
_STREAM_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class GitRepository:
    """One resolved repository directory and its trusted Git executable."""

    directory: Path
    executable: str


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded output and terminal state for one child process."""

    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_overflow: bool
    stderr_overflow: bool
    timed_out: bool


def open_repository(repository: Path) -> GitRepository:
    """Resolve a repository directory and an external regular Git executable."""
    if not isinstance(repository, Path):
        raise InputError("repository must be a filesystem path")
    try:
        directory = repository.expanduser().resolve(strict=True)
        metadata = directory.stat()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(repository, maximum=300)
        raise InputError(f"repository path cannot be resolved safely: {display}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        display = safe_error_text(directory, maximum=300)
        raise InputError(f"repository directory does not exist: {display}")

    trust_boundaries = _repository_trust_boundaries(directory)
    discovered_git = shutil.which("git")
    if discovered_git is None:
        raise GitError("git executable was not found")
    try:
        git_path = Path(discovered_git)
    except (TypeError, ValueError) as error:
        raise GitError("git executable path cannot be resolved safely") from error
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
    return GitRepository(directory=directory, executable=str(resolved_git))


def run_git(
    repository: GitRepository,
    arguments: Sequence[str],
    *,
    stdout_limit: int,
) -> ProcessResult:
    """Run one fixed Git operation without a shell, ambient config, or lazy fetch."""
    if not isinstance(repository, GitRepository):
        raise TypeError("repository must be an open GitRepository")
    if isinstance(arguments, (str, bytes)) or any(
        not isinstance(argument, str) for argument in arguments
    ):
        raise TypeError("git arguments must be a sequence of strings")
    if any("\0" in argument for argument in arguments):
        raise GitError("git arguments must not contain NUL bytes")
    command = [
        repository.executable,
        "--no-pager",
        "--no-lazy-fetch",
        "-C",
        str(repository.directory),
        *arguments,
    ]
    try:
        result = _run_bounded(
            command,
            stdout_limit=stdout_limit,
            stderr_limit=MAX_GIT_ERROR_BYTES,
            timeout_seconds=MAX_GIT_SECONDS,
        )
    except (OSError, ValueError) as error:
        detail = safe_error_text(error, maximum=300)
        raise GitError(f"could not execute git: {detail}") from error
    if result.timed_out:
        raise GitError(f"git command exceeded the hard {MAX_GIT_SECONDS:g}-second limit")
    if result.stdout_overflow:
        raise GitError(f"git output exceeds the hard {stdout_limit}-byte limit")
    if result.stderr_overflow:
        raise GitError(f"git error output exceeds the hard {MAX_GIT_ERROR_BYTES}-byte limit")
    return result


def safe_git_error(value: bytes) -> str:
    """Return one bounded, terminal-safe Git error detail."""
    decoded = value[:1000].decode("utf-8", errors="replace")
    return safe_error_text(decoded, maximum=1000) or "unknown git error"


def parse_object_id(output: bytes, *, label: str) -> str:
    """Parse one full lowercase SHA-1 or SHA-256 object identity."""
    lines = output.splitlines()
    if len(lines) != 1 or not _is_object_id(lines[0]):
        raise GitError(f"git returned a malformed {label} identity")
    return lines[0].decode("ascii")


def resolve_common_git_directory(repository: GitRepository) -> Path:
    """Resolve Git's common metadata directory from bounded command output."""
    result = run_git(
        repository,
        ["rev-parse", "--git-common-dir"],
        stdout_limit=_MAX_GIT_COMMON_DIR_BYTES,
    )
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        raise GitError(f"git could not locate common history metadata: {detail}")
    try:
        decoded = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise GitError("git returned malformed common history metadata") from error
    lines = decoded.splitlines()
    if len(lines) != 1 or not lines[0] or "\0" in lines[0]:
        raise GitError("git returned malformed common history metadata")
    unresolved = Path(lines[0])
    candidate = unresolved if unresolved.is_absolute() else repository.directory / unresolved
    try:
        common_directory = candidate.resolve(strict=True)
        metadata = common_directory.stat()
    except (OSError, RuntimeError, ValueError) as error:
        raise GitError("git common history metadata cannot be resolved safely") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise GitError("git common history metadata cannot be resolved safely")
    return common_directory


def require_complete_history(
    repository: GitRepository,
    *,
    shallow_message: str,
    grafts_message: str,
) -> Path:
    """Require non-shallow history without a legacy graft overlay."""
    common_directory = require_no_legacy_grafts(repository, message=grafts_message)
    result = run_git(
        repository,
        ["rev-parse", "--is-shallow-repository"],
        stdout_limit=16,
    )
    if result.returncode != 0:
        detail = safe_git_error(result.stderr)
        raise GitError(f"git could not determine whether history is complete: {detail}")
    answer = result.stdout.strip()
    if answer == b"true":
        raise GitError(shallow_message)
    if answer != b"false":
        raise GitError("git returned an invalid shallow-repository state")
    return common_directory


def require_no_legacy_grafts(repository: GitRepository, *, message: str) -> Path:
    """Require neutral legacy graft metadata without rejecting shallow history."""
    common_directory = resolve_common_git_directory(repository)
    _require_no_legacy_grafts(common_directory, message=message)
    return common_directory


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
    raw = _read_regular_metadata(
        path,
        label="repository object alternates",
        maximum=_MAX_GIT_METADATA_BYTES,
    )
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
    raw = _read_regular_metadata(
        path,
        label=label,
        maximum=_MAX_GIT_METADATA_BYTES,
    )
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


def _read_regular_metadata(path: Path, *, label: str, maximum: int) -> bytes:
    """Read bounded repository metadata without blocking on special files."""
    display = safe_error_text(path, maximum=300)
    try:
        before_open = path.lstat()
    except OSError as error:
        raise InputError(f"{label} cannot be read safely: {display}") from error
    if not stat.S_ISREG(before_open.st_mode):
        raise InputError(f"{label} must be a regular file")
    if before_open.st_size > maximum:
        raise InputError(f"{label} exceeds {maximum} bytes")

    flags = os.O_RDONLY
    for name in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, name, 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InputError(f"{label} cannot be read safely: {display}") from error

    try:
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise InputError(f"{label} must be a regular file")
            if (before_open.st_dev, before_open.st_ino) != (opened.st_dev, opened.st_ino):
                raise InputError(f"{label} changed while being opened")
            if opened.st_size > maximum:
                raise InputError(f"{label} exceeds {maximum} bytes")

            output = bytearray()
            while len(output) <= maximum:
                chunk = os.read(descriptor, min(_STREAM_CHUNK_BYTES, maximum + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
            if len(output) > maximum:
                raise InputError(f"{label} exceeds {maximum} bytes")
            return bytes(output)
        except OSError as error:
            raise InputError(f"{label} cannot be read safely: {display}") from error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise InputError(f"{label} cannot be closed safely: {display}") from error


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


def _require_no_legacy_grafts(common_directory: Path, *, message: str) -> None:
    grafts = common_directory / "info" / "grafts"
    try:
        metadata = grafts.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise GitError("git history overlay metadata cannot be validated safely") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise GitError("git history overlay metadata cannot be validated safely")
    if metadata.st_size:
        raise GitError(message)


def _is_object_id(value: bytes) -> bool:
    return len(value) in _OBJECT_ID_LENGTHS and all(byte in _LOWER_HEX for byte in value)


def _run_bounded(
    command: Sequence[str],
    *,
    stdout_limit: int,
    stderr_limit: int,
    timeout_seconds: float = MAX_GIT_SECONDS,
) -> ProcessResult:
    """Run one process while retaining at most the configured pipe bytes."""
    if stdout_limit < 1 or stderr_limit < 1 or timeout_seconds <= 0:
        raise ValueError("process output limits must be positive")
    containment = open_process_tree()
    try:
        process: subprocess.Popen[bytes] = subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            shell=False,
            text=False,
            creationflags=containment.creation_flags(),
            start_new_session=containment.starts_new_session(),
        )
    except BaseException:
        containment.close()
        raise
    try:
        containment.attach_and_start(process)
    except BaseException as error:
        cleanup_errors = _abort_unstarted_process(process, containment)
        if cleanup_errors:
            raise OSError("could not clean up an uncontained process") from cleanup_errors[0]
        raise OSError("could not establish process-tree containment") from error
    if process.stdout is None or process.stderr is None:
        cleanup_errors = _abort_unstarted_process(process, containment)
        if cleanup_errors:
            raise OSError("could not clean up a process without output pipes") from cleanup_errors[
                0
            ]
        raise OSError("bounded process did not expose both output pipes")
    process_streams = (process.stdout, process.stderr)

    stdout = bytearray()
    stderr = bytearray()
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    reader_errors: list[OSError] = []
    termination_errors: list[OSError] = []
    termination_requested = threading.Event()
    termination_lock = threading.Lock()

    def terminate_process_tree() -> None:
        with termination_lock:
            if termination_requested.is_set():
                return
            termination_requested.set()
            try:
                containment.terminate(process)
            except OSError as error:
                termination_errors.append(error)

    threads: list[threading.Thread] = []
    started_stream_ids: set[int] = set()
    operation_deadline = time.monotonic() + timeout_seconds
    timed_out = False
    cleaned_up = False
    try:
        reader_specs = (
            (process.stdout, stdout_limit, stdout, stdout_overflow),
            (process.stderr, stderr_limit, stderr, stderr_overflow),
        )
        for stream, limit, destination, overflow in reader_specs:
            thread = threading.Thread(
                target=_capture_stream,
                args=(
                    stream,
                    limit,
                    destination,
                    overflow,
                    terminate_process_tree,
                    reader_errors,
                ),
                daemon=True,
            )
            thread.start()
            threads.append(thread)
            started_stream_ids.add(id(stream))

        try:
            returncode = process.wait(timeout=_remaining_seconds(operation_deadline))
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_tree()
            returncode = process.returncode

        if not termination_requested.is_set() and not _join_threads(
            threads, deadline=operation_deadline
        ):
            timed_out = True
            terminate_process_tree()

        if termination_requested.is_set():
            returncode, cleanup_errors = _cleanup_process_tree(
                process,
                containment,
                threads,
                terminate_process_tree,
            )
            cleaned_up = True
            cleanup_errors.extend(termination_errors)
            if cleanup_errors:
                raise OSError("could not clean up the bounded process tree") from cleanup_errors[0]
        else:
            containment.close()
            cleaned_up = True

        if reader_errors:
            raise OSError("could not read process output") from reader_errors[0]
        if returncode is None:
            raise OSError("bounded process did not report a terminal status")
        return ProcessResult(
            returncode=returncode,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            stdout_overflow=stdout_overflow.is_set(),
            stderr_overflow=stderr_overflow.is_set(),
            timed_out=timed_out,
        )
    except BaseException:
        if not cleaned_up:
            _, cleanup_errors = _cleanup_process_tree(
                process,
                containment,
                threads,
                terminate_process_tree,
            )
            cleaned_up = True
            cleanup_errors.extend(termination_errors)
            if cleanup_errors:
                raise OSError("could not clean up the bounded process tree") from cleanup_errors[0]
        raise
    finally:
        for stream in process_streams:
            if id(stream) not in started_stream_ids:
                stream.close()
        if not cleaned_up:
            containment.close()


def _abort_unstarted_process(
    process: subprocess.Popen[bytes],
    containment: ProcessTree,
) -> list[Exception]:
    errors: list[Exception] = []
    try:
        process.kill()
    except OSError as error:
        errors.append(error)
    try:
        containment.close()
    except OSError as error:
        errors.append(error)
    try:
        process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        errors.append(error)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    return errors


def _cleanup_process_tree(
    process: subprocess.Popen[bytes],
    containment: ProcessTree,
    threads: list[threading.Thread],
    terminate_process_tree: Callable[[], None],
) -> tuple[int | None, list[Exception]]:
    errors: list[Exception] = []
    terminate_process_tree()
    try:
        containment.close()
    except OSError as error:
        errors.append(error)
    cleanup_deadline = time.monotonic() + _PROCESS_CLEANUP_SECONDS
    returncode = process.returncode
    if returncode is None:
        try:
            returncode = process.wait(timeout=_remaining_seconds(cleanup_deadline))
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append(error)
    if not _join_threads(threads, deadline=cleanup_deadline):
        errors.append(OSError("process output readers did not stop within the cleanup limit"))
    return returncode, errors


def _join_threads(threads: list[threading.Thread], *, deadline: float) -> bool:
    for thread in threads:
        thread.join(timeout=_remaining_seconds(deadline))
    return all(not thread.is_alive() for thread in threads)


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


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
        try:
            stream.close()
        except OSError as error:
            reader_errors.append(error)
            kill_process()


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
