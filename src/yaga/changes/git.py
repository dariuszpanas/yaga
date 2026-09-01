"""Bounded, shell-free Git changed-path selection."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO

from yaga.changes.models import (
    MAX_CHANGED_PATH_BYTES,
    MAX_CHANGED_PATH_COMPONENTS,
    MAX_CHANGED_PATHS,
    MAX_REVISION_RANGE_CHARS,
    ChangeSelection,
)
from yaga.errors import GitError, safe_error_text

MAX_REVISION_CHARS = MAX_REVISION_RANGE_CHARS
MAX_GIT_DIFF_BYTES = 4 * 1024 * 1024
MAX_GIT_ERROR_BYTES = 64 * 1024
MAX_GIT_SECONDS = 30.0

_MAX_GIT_IDENTITY_BYTES = 256
_MAX_GIT_COMMON_DIR_BYTES = 32 * 1024
_MAX_MERGE_BASE_BYTES = 4096
_STREAM_CHUNK_BYTES = 64 * 1024
_OBJECT_ID_LENGTHS = frozenset({40, 64})
_LOWER_HEX = frozenset(b"0123456789abcdef")
_RANGE_PATTERN = re.compile(r"^(.+?)(\.\.\.|\.\.)(.+)$")
_SET_OPERATOR_PATTERN = re.compile(r"\^(?:@|!|-(?:[0-9]+)?)")
_UNSAFE_PATH_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_PROCESS_ENVIRONMENT_ALLOWLIST = frozenset({"SYSTEMROOT", "WINDIR"})


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_overflow: bool
    stderr_overflow: bool
    timed_out: bool


def read_changed_paths(repository: Path, revision_range: str) -> ChangeSelection:
    """Resolve an exact range and return its bounded changed repository paths."""
    base, separator, head = _split_explicit_range(revision_range)
    repo, git = _repository_and_git(repository)
    common_git_dir = _require_complete_history(repo, git)
    base_sha = _resolve_commitish(repo, git, base, source="range base")
    head_sha = _resolve_commitish(repo, git, head, source="range head")
    if len(base_sha) != len(head_sha):
        raise GitError("git returned inconsistent commit identity lengths")

    comparison_sha = base_sha
    if separator == "...":
        comparison_sha = _resolve_unique_merge_base(repo, git, base_sha, head_sha)
        if len(comparison_sha) != len(base_sha):
            raise GitError("git returned an inconsistent merge-base identity length")

    paths = _read_changed_path_output(repo, git, comparison_sha, head_sha)
    _require_no_legacy_grafts(common_git_dir)
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


def _repository_and_git(repository: Path) -> tuple[Path, str]:
    try:
        repo = repository.expanduser().resolve()
        is_directory = repo.is_dir()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(repository, maximum=300)
        raise GitError(f"repository path cannot be resolved safely: {display}") from error
    if not is_directory:
        display = safe_error_text(repo, maximum=300)
        raise GitError(f"repository directory does not exist: {display}")
    discovered_git = shutil.which("git")
    if discovered_git is None:
        raise GitError("git executable was not found")
    git_path = Path(discovered_git)
    if not git_path.is_absolute():
        raise GitError("git executable path must be absolute")
    if _is_within(git_path, repo):
        raise GitError("git executable must not be discovered inside the repository")
    try:
        resolved_git = git_path.resolve()
        is_file = resolved_git.is_file()
    except (OSError, RuntimeError, ValueError) as error:
        raise GitError("git executable path cannot be resolved safely") from error
    if not is_file:
        raise GitError("git executable is not a regular file")
    if _is_within(resolved_git, repo):
        raise GitError("git executable must not resolve inside the repository")
    return repo, str(resolved_git)


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _require_complete_history(repo: Path, git: str) -> Path:
    common_git_dir = _resolve_common_git_directory(repo, git)
    _require_no_legacy_grafts(common_git_dir)
    result = _run_git(
        [git, "-C", str(repo), "rev-parse", "--is-shallow-repository"],
        stdout_limit=16,
    )
    if result.returncode != 0:
        detail = _safe_git_error(result.stderr)
        raise GitError(f"git could not determine whether history is complete: {detail}")
    answer = result.stdout.strip()
    if answer == b"true":
        raise GitError("changed paths require a non-shallow repository with complete history")
    if answer != b"false":
        raise GitError("git returned an invalid shallow-repository state")
    return common_git_dir


def _resolve_common_git_directory(repo: Path, git: str) -> Path:
    result = _run_git(
        [
            git,
            "-C",
            str(repo),
            "rev-parse",
            "--git-common-dir",
        ],
        stdout_limit=_MAX_GIT_COMMON_DIR_BYTES,
    )
    if result.returncode != 0:
        detail = _safe_git_error(result.stderr)
        raise GitError(f"git could not locate common history metadata: {detail}")
    try:
        decoded = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise GitError("git returned malformed common history metadata") from error
    lines = decoded.splitlines()
    if len(lines) != 1 or not lines[0] or "\0" in lines[0]:
        raise GitError("git returned malformed common history metadata")
    unresolved = Path(lines[0])
    candidate = unresolved if unresolved.is_absolute() else repo / unresolved
    try:
        common_git_dir = candidate.resolve(strict=True)
        mode = common_git_dir.stat().st_mode
    except (OSError, RuntimeError, ValueError) as error:
        raise GitError("git common history metadata cannot be resolved safely") from error
    if not stat.S_ISDIR(mode):
        raise GitError("git common history metadata cannot be resolved safely")
    return common_git_dir


def _require_no_legacy_grafts(common_git_dir: Path) -> None:
    grafts = common_git_dir / "info" / "grafts"
    try:
        metadata = grafts.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise GitError("git history overlay metadata cannot be validated safely") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise GitError("git history overlay metadata cannot be validated safely")
    if metadata.st_size:
        raise GitError("changed paths require complete history without legacy graft overlays")


def _resolve_commitish(repo: Path, git: str, revision: str, *, source: str) -> str:
    result = _run_git(
        [
            git,
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ],
        stdout_limit=_MAX_GIT_IDENTITY_BYTES,
    )
    if result.returncode != 0:
        detail = _safe_git_error(result.stderr)
        label = safe_error_text(revision, maximum=160)
        raise GitError(f"git could not resolve {source} {label!r} as one commit: {detail}")
    return _parse_single_identity(result.stdout, label="commit")


def _resolve_unique_merge_base(repo: Path, git: str, base_sha: str, head_sha: str) -> str:
    result = _run_git(
        [git, "-C", str(repo), "merge-base", "--all", base_sha, head_sha],
        stdout_limit=_MAX_MERGE_BASE_BYTES,
    )
    if result.returncode != 0:
        detail = _safe_git_error(result.stderr)
        raise GitError(f"git could not determine one merge base: {detail}")
    lines = result.stdout.splitlines()
    if len(lines) > 1:
        raise GitError("three-dot changed paths require exactly one merge base")
    if len(lines) != 1 or not _is_object_id(lines[0]):
        raise GitError("git returned a malformed merge-base identity")
    return lines[0].decode("ascii")


def _parse_single_identity(output: bytes, *, label: str) -> str:
    lines = output.splitlines()
    if len(lines) != 1 or not _is_object_id(lines[0]):
        raise GitError(f"git returned a malformed {label} identity")
    return lines[0].decode("ascii")


def _is_object_id(value: bytes) -> bool:
    return len(value) in _OBJECT_ID_LENGTHS and all(byte in _LOWER_HEX for byte in value)


def _read_changed_path_output(
    repo: Path,
    git: str,
    comparison_sha: str,
    head_sha: str,
) -> tuple[str, ...]:
    result = _run_git(
        [
            git,
            "-C",
            str(repo),
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
        detail = _safe_git_error(result.stderr)
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
