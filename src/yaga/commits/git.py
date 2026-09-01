"""Bounded, shell-free Git commit selection."""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from yaga.commits.models import CommitTarget
from yaga.errors import GitError

MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_GIT_ERROR_BYTES = 64 * 1024
MAX_GIT_IDENTITY_BYTES = 256
MAX_REVISION_CHARS = 512
_STREAM_CHUNK_BYTES = 64 * 1024
_OBJECT_ID_LENGTHS = frozenset({40, 64})
_LOWER_HEX = frozenset(b"0123456789abcdef")
_RANGE_PATTERN = re.compile(r"^(.+?)(\.\.\.|\.\.)(.+)$")
_SET_OPERATOR_PATTERN = re.compile(r"\^(?:@|!|-(?:[0-9]+)?)")


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_overflow: bool
    stderr_overflow: bool


def read_commit(repository: Path, revision: str = "HEAD") -> CommitTarget:
    """Resolve one commit-ish and read that commit's complete message."""
    repo, git = _repository_and_git(repository)
    resolved = _resolve_commitish(repo, git, revision, source="--commit")
    commits = _read_log(
        repo,
        git,
        resolved,
        max_commits=1,
        reverse=False,
        detect_overflow=False,
        no_walk=True,
    )
    if not commits:
        raise GitError(f"revision {_safe_label(revision)!r} did not select a commit")
    return commits[0]


def read_range(repository: Path, revision_range: str, *, max_commits: int) -> list[CommitTarget]:
    """Resolve an explicit range and read its commits in oldest-first order."""
    if max_commits < 1:
        raise GitError("maximum commit count must be positive")
    base, separator, head = _split_explicit_range(revision_range)
    repo, git = _repository_and_git(repository)
    _require_complete_history(repo, git)
    resolved_base = _resolve_commitish(repo, git, base, source="--range base")
    resolved_head = _resolve_commitish(repo, git, head, source="--range head")
    selection = f"{resolved_base}{separator}{resolved_head}"
    commits = _read_log(
        repo,
        git,
        selection,
        max_commits=max_commits,
        reverse=True,
        detect_overflow=True,
        no_walk=False,
    )
    if not commits:
        raise GitError(f"revision range {_safe_label(revision_range)!r} selected no commits")
    return commits


def _require_complete_history(repo: Path, git: str) -> None:
    result = _run_git(
        [git, "-C", str(repo), "rev-parse", "--is-shallow-repository"],
        stdout_limit=16,
    )
    if result.returncode != 0:
        detail = _safe_git_error(result.stderr)
        raise GitError(f"git could not determine whether history is complete: {detail}")
    answer = result.stdout.strip()
    if answer == b"true":
        raise GitError("--range requires a non-shallow repository with complete history")
    if answer != b"false":
        raise GitError("git returned an invalid shallow-repository state")


def _repository_and_git(repository: Path) -> tuple[Path, str]:
    repo = repository.expanduser().resolve()
    if not repo.is_dir():
        raise GitError(f"repository directory does not exist: {_safe_label(str(repo))}")
    git = shutil.which("git")
    if git is None:
        raise GitError("git executable was not found")
    return repo, git


def _resolve_commitish(repo: Path, git: str, revision: str, *, source: str) -> str:
    _validate_commitish(revision, source=source)
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
        stdout_limit=MAX_GIT_IDENTITY_BYTES,
    )
    if result.returncode != 0:
        detail = _safe_git_error(result.stderr)
        raise GitError(f"git could not resolve {_safe_label(revision)!r} as one commit: {detail}")
    lines = result.stdout.splitlines()
    if len(lines) != 1 or not _is_object_id(lines[0]):
        raise GitError("git returned a malformed commit identity")
    return lines[0].decode("ascii")


def _read_log(
    repo: Path,
    git: str,
    selection: str,
    *,
    max_commits: int,
    reverse: bool,
    detect_overflow: bool,
    no_walk: bool,
) -> list[CommitTarget]:
    command = [
        git,
        "-C",
        str(repo),
        "log",
        "--no-show-signature",
        "--format=%H%x00%P%x00%B",
        "-z",
        f"--max-count={max_commits + int(detect_overflow)}",
    ]
    if reverse:
        command.append("--reverse")
    if no_walk:
        command.append("--no-walk")
    command.extend([selection, "--"])
    result = _run_git(command, stdout_limit=MAX_GIT_OUTPUT_BYTES)
    if result.returncode != 0:
        detail = _safe_git_error(result.stderr)
        raise GitError(f"git could not read the resolved commit selection: {detail}")

    commits = _parse_records(result.stdout)
    if detect_overflow and len(commits) > max_commits:
        raise GitError(f"commit selection exceeds the configured limit of {max_commits}")
    return commits


def _run_git(command: list[str], *, stdout_limit: int) -> _ProcessResult:
    try:
        result = _run_bounded(
            command,
            stdout_limit=stdout_limit,
            stderr_limit=MAX_GIT_ERROR_BYTES,
        )
    except OSError as error:
        raise GitError(f"could not execute git: {_safe_label(str(error))}") from error
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
) -> _ProcessResult:
    """Run a process while retaining at most the configured bytes from each pipe."""
    if stdout_limit < 1 or stderr_limit < 1:
        raise ValueError("process output limits must be positive")
    process = subprocess.Popen(  # noqa: S603 - fixed executable, no shell
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
        if not _is_object_id(sha_bytes):
            raise GitError("git returned a malformed commit identity")
        sha = sha_bytes.decode("ascii")
        parents = _parse_parent_ids(fields[index + 1], object_id_length=len(sha_bytes))
        message = fields[index + 2].decode("utf-8", errors="replace")
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
    if any(len(parent) != object_id_length or not _is_object_id(parent) for parent in raw_parents):
        raise GitError("git returned a malformed parent identity")
    return tuple(parent.decode("ascii") for parent in raw_parents)


def _is_object_id(value: bytes) -> bool:
    return len(value) in _OBJECT_ID_LENGTHS and all(byte in _LOWER_HEX for byte in value)


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
        ".." in revision
        or revision.startswith("^")
        or _SET_OPERATOR_PATTERN.search(revision) is not None
    ):
        raise GitError(f"{source} requires exactly one commit-ish, not a revision set")


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


def _safe_git_error(value: bytes) -> str:
    decoded = value[:1000].decode("utf-8", errors="replace")
    return " ".join(_safe_label(decoded).split()) or "unknown git error"
