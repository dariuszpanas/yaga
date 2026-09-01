"""Tests for the shared bounded Git repository and process runtime."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from yaga.errors import GitError, InputError
from yaga.git import (
    GitRepository,
    ProcessResult,
    open_repository,
    parse_object_id,
    require_complete_history,
    resolve_common_git_directory,
    run_git,
    runtime,
    safe_git_error,
)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def init_repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.email", "yaga@example.invalid")
    git(path, "config", "user.name", "YAGA Tests")
    (path / "README.md").write_text("test\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "--quiet", "-m", "chore: initialize")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    init_repository(tmp_path)
    return tmp_path


def test_opens_enclosing_repository_and_runs_git_with_fixed_global_options(
    repository: Path,
) -> None:
    nested = repository / "nested"
    nested.mkdir()
    selected = open_repository(nested / "..")

    assert selected.directory == repository.resolve()
    executable = Path(selected.executable)
    assert executable.is_absolute()
    assert stat.S_ISREG(executable.stat().st_mode)

    result = run_git(selected, ["rev-parse", "--verify", "HEAD"], stdout_limit=256)

    assert result.returncode == 0
    assert parse_object_id(result.stdout, label="commit") == git(repository, "rev-parse", "HEAD")


def test_repository_path_must_be_a_resolved_directory(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="filesystem path"):
        open_repository(cast(Path, "."))

    with pytest.raises(InputError, match="cannot be resolved safely"):
        open_repository(tmp_path / "missing")

    regular_file = tmp_path / "file"
    regular_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(InputError, match="directory does not exist"):
        open_repository(regular_file)


def test_git_executable_must_be_absolute_regular_and_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as context:
        context.setattr(runtime.shutil, "which", lambda _name: None)
        with pytest.raises(GitError, match="was not found"):
            open_repository(tmp_path)

    with monkeypatch.context() as context:
        context.setattr(runtime.shutil, "which", lambda _name: "tools/git")
        with pytest.raises(GitError, match="path must be absolute"):
            open_repository(tmp_path)

    with monkeypatch.context() as context:
        context.setattr(runtime.shutil, "which", lambda _name: str(tmp_path.parent))
        with pytest.raises(GitError, match="not a regular file"):
            open_repository(tmp_path)


def test_git_executable_inside_enclosing_worktree_is_rejected_from_subdirectory(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subdirectory = repository / "src"
    subdirectory.mkdir()
    untrusted_git = repository / "tools" / "git"
    untrusted_git.parent.mkdir()
    untrusted_git.write_bytes(b"untrusted")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: str(untrusted_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        open_repository(subdirectory)


def test_git_executable_inside_configless_bare_repository_is_rejected(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare = tmp_path.with_name(f"{tmp_path.name}-bare.git")
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(repository), str(bare)],
        check=True,
        capture_output=True,
    )
    (bare / "config").unlink()
    untrusted_git = bare / "git.exe"
    untrusted_git.write_bytes(b"untrusted")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: str(untrusted_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        open_repository(bare / "objects")


def test_git_executable_inside_separate_git_directory_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    metadata = tmp_path / "metadata"
    subprocess.run(
        [
            "git",
            "init",
            "--quiet",
            "--initial-branch=main",
            f"--separate-git-dir={metadata}",
            str(worktree),
        ],
        check=True,
        capture_output=True,
    )
    untrusted_git = metadata / "git.exe"
    untrusted_git.write_bytes(b"untrusted")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: str(untrusted_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        open_repository(worktree)


def test_git_executable_inside_linked_worktree_common_directory_is_rejected(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked = tmp_path.with_name(f"{tmp_path.name}-linked")
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "--quiet", "--detach", str(linked)],
        check=True,
        capture_output=True,
    )
    untrusted_git = repository / ".git" / "git.exe"
    untrusted_git.write_bytes(b"untrusted")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: str(untrusted_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        open_repository(linked)


def test_git_executable_inside_alternate_object_database_is_rejected(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path.with_name(f"{tmp_path.name}-shared")
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", str(repository), str(shared)],
        check=True,
        capture_output=True,
    )
    untrusted_git = repository / ".git" / "objects" / "git.exe"
    untrusted_git.write_bytes(b"untrusted")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: str(untrusted_git.resolve()))

    with pytest.raises(GitError, match="inside the repository"):
        open_repository(shared)


def test_git_metadata_pointers_are_bounded_and_single_line(tmp_path: Path) -> None:
    marker = tmp_path / ".git"
    marker.write_bytes(b"x" * (runtime._MAX_GIT_METADATA_BYTES + 1))
    with pytest.raises(InputError, match="exceeds"):
        open_repository(tmp_path)

    marker.write_text("gitdir: metadata\nsecond-line\n", encoding="utf-8")
    with pytest.raises(InputError, match="one bounded path"):
        open_repository(tmp_path)


@pytest.mark.parametrize(
    "relative_path",
    [Path(".git/commondir"), Path(".git/objects/info/alternates")],
)
def test_git_metadata_pointers_must_be_regular_files(
    repository: Path,
    relative_path: Path,
) -> None:
    metadata = repository / relative_path
    metadata.mkdir(parents=True)

    with pytest.raises(InputError, match="must be a regular file"):
        open_repository(repository)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFOs are not available on Windows")
@pytest.mark.parametrize(
    "relative_path",
    [Path(".git/commondir"), Path(".git/objects/info/alternates")],
)
def test_git_metadata_fifos_fail_closed_without_blocking(
    repository: Path,
    relative_path: Path,
) -> None:
    metadata = repository / relative_path
    metadata.parent.mkdir(parents=True, exist_ok=True)
    make_fifo = getattr(os, "mkfifo", None)
    assert callable(make_fifo)
    make_fifo(metadata)

    before = time.monotonic()
    with pytest.raises(InputError, match="must be a regular file"):
        open_repository(repository)
    elapsed = time.monotonic() - before

    assert elapsed < 1.0


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFOs are not available on Windows")
def test_git_metadata_fifo_replacement_during_open_does_not_block(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = repository / ".git" / "commondir"
    metadata.write_text(".\n", encoding="utf-8")
    make_fifo = getattr(os, "mkfifo", None)
    assert callable(make_fifo)
    real_open = runtime.os.open
    replaced = False

    def replace_with_fifo_before_open(path: Any, flags: int) -> int:
        nonlocal replaced
        if path == metadata and not replaced:
            metadata.unlink()
            make_fifo(metadata)
            replaced = True
        return real_open(path, flags)

    monkeypatch.setattr(runtime.os, "open", replace_with_fifo_before_open)

    before = time.monotonic()
    with pytest.raises(InputError, match="must be a regular file"):
        open_repository(repository)
    elapsed = time.monotonic() - before

    assert replaced
    assert elapsed < 1.0


def test_git_metadata_replacement_during_open_fails_closed(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = repository / ".git" / "commondir"
    replacement = repository / ".git" / "replacement-commondir"
    metadata.write_text(".\n", encoding="utf-8")
    replacement.write_text(".\n", encoding="utf-8")
    real_open = runtime.os.open
    replaced = False

    def replace_before_open(path: Any, flags: int) -> int:
        nonlocal replaced
        if path == metadata and not replaced:
            replacement.replace(metadata)
            replaced = True
        return real_open(path, flags)

    monkeypatch.setattr(runtime.os, "open", replace_before_open)

    with pytest.raises(InputError, match="changed while being opened"):
        open_repository(repository)
    assert replaced


def test_alternate_object_cycles_terminate_and_cumulative_limit_fails_closed(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory, alternate in ((first, second), (second, first)):
        info = directory / "info"
        info.mkdir(parents=True)
        (info / "alternates").write_text(f"{alternate}\n", encoding="utf-8")

    assert runtime._git_object_boundaries(first) == (first.resolve(), second.resolve())

    chain = [tmp_path / f"chain-{index}" for index in range(129)]
    for index, directory in enumerate(chain):
        (directory / "info").mkdir(parents=True)
        if index + 1 < len(chain):
            (directory / "info" / "alternates").write_text(
                f"{chain[index + 1]}\n",
                encoding="utf-8",
            )
    with pytest.raises(InputError, match="128-directory limit"):
        runtime._git_object_boundaries(chain[0])


def test_alternate_object_metadata_rejects_empty_records_and_oversize(
    tmp_path: Path,
) -> None:
    objects = tmp_path / "objects"
    info = objects / "info"
    info.mkdir(parents=True)
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    metadata = info / "alternates"

    metadata.write_text(f"{alternate}\n\n{alternate}\n", encoding="utf-8")
    with pytest.raises(InputError, match="one path per line"):
        runtime._git_object_boundaries(objects)

    metadata.write_bytes(b"x" * (runtime._MAX_GIT_METADATA_BYTES + 1))
    with pytest.raises(InputError, match="exceed"):
        runtime._git_object_boundaries(objects)


@pytest.mark.parametrize("length", [40, 64])
def test_object_identity_parser_accepts_supported_lowercase_hashes(length: int) -> None:
    identity = b"a" * length

    assert parse_object_id(identity + b"\n", label="commit") == identity.decode()


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"a" * 39 + b"\n",
        b"a" * 65 + b"\n",
        b"A" * 40 + b"\n",
        b"g" * 40 + b"\n",
        b"a" * 40 + b"\n" + b"b" * 40 + b"\n",
    ],
)
def test_object_identity_parser_rejects_malformed_or_multiple_values(output: bytes) -> None:
    with pytest.raises(GitError, match="malformed commit identity"):
        parse_object_id(output, label="commit")


def test_run_git_builds_fixed_global_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    expected = ProcessResult(0, b"ok", b"", False, False, False)

    def fake_run_bounded(
        command: list[str],
        *,
        stdout_limit: int,
        stderr_limit: int,
        timeout_seconds: float,
    ) -> ProcessResult:
        captured.update(
            command=command,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            timeout_seconds=timeout_seconds,
        )
        return expected

    monkeypatch.setattr(runtime, "_run_bounded", fake_run_bounded)
    repository = GitRepository(tmp_path.resolve(), str((tmp_path.parent / "git.exe").resolve()))

    result = run_git(repository, ["rev-parse", "--verify", "HEAD"], stdout_limit=123)

    assert result is expected
    assert captured["command"] == [
        repository.executable,
        "--no-pager",
        "--no-lazy-fetch",
        "-C",
        str(repository.directory),
        "rev-parse",
        "--verify",
        "HEAD",
    ]
    assert captured["stdout_limit"] == 123
    assert captured["stderr_limit"] == runtime.MAX_GIT_ERROR_BYTES
    assert captured["timeout_seconds"] == runtime.MAX_GIT_SECONDS


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (ProcessResult(1, b"", b"", True, False, False), "hard 8-byte limit"),
        (
            ProcessResult(1, b"", b"", False, True, False),
            f"hard {runtime.MAX_GIT_ERROR_BYTES}-byte limit",
        ),
        (
            ProcessResult(1, b"", b"", False, False, True),
            f"hard {runtime.MAX_GIT_SECONDS:g}-second limit",
        ),
    ],
)
def test_run_git_translates_process_limits(
    result: ProcessResult,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runtime, "_run_bounded", lambda *_args, **_kwargs: result)
    repository = GitRepository(tmp_path.resolve(), sys.executable)

    with pytest.raises(GitError, match=message):
        run_git(repository, ["version"], stdout_limit=8)


def test_run_git_translates_process_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> ProcessResult:
        raise OSError("bad\n\x1b[31mspawn")

    monkeypatch.setattr(runtime, "_run_bounded", fail)

    with pytest.raises(GitError, match="could not execute git") as raised:
        run_git(GitRepository(tmp_path.resolve(), sys.executable), ["version"], stdout_limit=8)
    assert "\n" not in str(raised.value)
    assert "\x1b" not in str(raised.value)


def test_run_git_rejects_nul_arguments_before_process_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        runtime,
        "_run_bounded",
        lambda *_args, **_kwargs: pytest.fail("unsafe arguments must not reach process creation"),
    )

    with pytest.raises(GitError, match="must not contain NUL"):
        run_git(
            GitRepository(tmp_path.resolve(), sys.executable),
            ["version\0unsafe"],
            stdout_limit=8,
        )


def test_bounded_process_capture_enforces_both_stream_limits_and_timeout() -> None:
    for file_descriptor, overflow_attribute, output_attribute in (
        (1, "stdout_overflow", "stdout"),
        (2, "stderr_overflow", "stderr"),
    ):
        result = runtime._run_bounded(
            [
                sys.executable,
                "-c",
                f"import os; os.write({file_descriptor}, b'x' * 4096)",
            ],
            stdout_limit=128,
            stderr_limit=128,
        )
        assert getattr(result, overflow_attribute)
        assert len(getattr(result, output_attribute)) == 128
        assert result.timed_out is False

    timed_out = runtime._run_bounded(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout_limit=16,
        stderr_limit=16,
        timeout_seconds=0.1,
    )
    assert timed_out.timed_out


def test_timeout_terminates_pipe_holding_descendants_within_a_bounded_cleanup(
    tmp_path: Path,
) -> None:
    started = tmp_path / "timeout-child-started"
    survived = tmp_path / "timeout-child-survived"
    child_code = (
        "import pathlib,time; "
        f"pathlib.Path({str(started)!r}).write_text('started'); "
        "time.sleep(1.0); "
        f"pathlib.Path({str(survived)!r}).write_text('survived')"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"started=pathlib.Path({str(started)!r}); "
        "deadline=time.monotonic()+5; "
        "\nwhile not started.exists() and time.monotonic() < deadline: time.sleep(0.01); "
        "\nprint('spawned', flush=True)"
    )

    before = time.monotonic()
    result = runtime._run_bounded(
        [sys.executable, "-c", parent_code],
        stdout_limit=1024,
        stderr_limit=1024,
        timeout_seconds=0.6,
    )
    elapsed = time.monotonic() - before

    assert result.timed_out
    expected_output = b"spawned\r\n" if os.name == "nt" else b"spawned\n"
    assert result.stdout == expected_output
    assert elapsed < 2.0
    assert started.exists()
    time.sleep(0.6)
    assert not survived.exists()


def test_normal_completion_closes_containment_and_kills_silent_descendants(
    tmp_path: Path,
) -> None:
    started = tmp_path / "normal-child-started"
    survived = tmp_path / "normal-child-survived"
    child_code = (
        "import pathlib,time; "
        f"pathlib.Path({str(started)!r}).write_text('started'); "
        "time.sleep(1.0); "
        f"pathlib.Path({str(survived)!r}).write_text('survived')"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"started=pathlib.Path({str(started)!r}); "
        "deadline=time.monotonic()+5; "
        "\nwhile not started.exists() and time.monotonic() < deadline: time.sleep(0.01); "
        "\nprint('spawned', flush=True)"
    )

    before = time.monotonic()
    result = runtime._run_bounded(
        [sys.executable, "-c", parent_code],
        stdout_limit=1024,
        stderr_limit=1024,
        timeout_seconds=6.0,
    )
    elapsed = time.monotonic() - before

    assert result.returncode == 0
    assert result.timed_out is False
    expected_output = b"spawned\r\n" if os.name == "nt" else b"spawned\n"
    assert result.stdout == expected_output
    assert elapsed < 2.0
    assert started.exists()
    time.sleep(1.1)
    assert not survived.exists()


def test_cleanup_failure_never_closes_a_pipe_behind_a_blocked_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_reader = runtime.threading.Event()
    reader_finished = runtime.threading.Event()
    closed_while_blocked = False

    class BlockingStream:
        def read(self, _size: int) -> bytes:
            release_reader.wait(timeout=5)
            return b""

        def close(self) -> None:
            nonlocal closed_while_blocked
            if not release_reader.is_set():
                closed_while_blocked = True
                raise AssertionError("a blocked reader stream must not be closed concurrently")
            reader_finished.set()

    class EmptyStream:
        def read(self, _size: int) -> bytes:
            return b""

        def close(self) -> None:
            return None

    class Process:
        stdout = BlockingStream()
        stderr = EmptyStream()
        returncode = None
        pid = 123

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(["fake"], timeout or 0.0)

        def kill(self) -> None:
            return None

    class Containment:
        def creation_flags(self) -> int:
            return 0

        def starts_new_session(self) -> bool:
            return False

        def attach_and_start(self, _process: object) -> None:
            return None

        def terminate(self, _process: object) -> None:
            raise OSError("tree termination failed")

        def close(self) -> None:
            raise OSError("containment close failed")

    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(runtime, "open_process_tree", Containment)
    monkeypatch.setattr(runtime, "_PROCESS_CLEANUP_SECONDS", 0.05)

    before = time.monotonic()
    try:
        with pytest.raises(OSError, match="could not clean up the bounded process tree"):
            runtime._run_bounded(
                ["fake"],
                stdout_limit=16,
                stderr_limit=16,
                timeout_seconds=0.01,
            )
    finally:
        release_reader.set()
    elapsed = time.monotonic() - before

    assert elapsed < 0.5
    assert closed_while_blocked is False
    assert reader_finished.wait(timeout=1)


def test_reader_thread_setup_failure_cleans_up_the_running_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    survived = tmp_path / "thread-setup-survived"
    command = [
        sys.executable,
        "-c",
        (
            "import pathlib,time; time.sleep(1); "
            f"pathlib.Path({str(survived)!r}).write_text('survived')"
        ),
    ]
    real_thread = runtime.threading.Thread
    constructions = 0

    def fail_second_reader(**kwargs: Any) -> runtime.threading.Thread:
        nonlocal constructions
        constructions += 1
        if constructions == 2:
            raise RuntimeError("reader thread unavailable")
        return real_thread(**kwargs)

    monkeypatch.setattr(runtime.threading, "Thread", fail_second_reader)

    before = time.monotonic()
    with pytest.raises(RuntimeError, match="reader thread unavailable"):
        runtime._run_bounded(
            command,
            stdout_limit=16,
            stderr_limit=16,
            timeout_seconds=5,
        )
    elapsed = time.monotonic() - before

    assert constructions == 2
    assert elapsed < 2
    time.sleep(1.1)
    assert not survived.exists()


def test_reader_stream_close_errors_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BadCloseStream:
        def read(self, _size: int) -> bytes:
            return b""

        def close(self) -> None:
            raise OSError("pipe close failed")

    class EmptyStream:
        def read(self, _size: int) -> bytes:
            return b""

        def close(self) -> None:
            return None

    class Process:
        stdout = BadCloseStream()
        stderr = EmptyStream()
        returncode = 0
        pid = 123

        def poll(self) -> int:
            return 0

        def wait(self, *, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return None

    class Containment:
        def creation_flags(self) -> int:
            return 0

        def starts_new_session(self) -> bool:
            return False

        def attach_and_start(self, _process: object) -> None:
            return None

        def terminate(self, _process: object) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(runtime, "open_process_tree", Containment)

    with pytest.raises(OSError, match="could not read process output"):
        runtime._run_bounded(
            ["fake"],
            stdout_limit=16,
            stderr_limit=16,
            timeout_seconds=1,
        )


def test_bounded_process_uses_no_shell_devnull_stdin_and_minimal_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Containment:
        def creation_flags(self) -> int:
            return 0

        def starts_new_session(self) -> bool:
            return False

        def attach_and_start(self, _process: object) -> None:
            captured["attached"] = True

        def terminate(self, _process: object) -> None:
            raise AssertionError("completed process tree must not be terminated")

        def close(self) -> None:
            captured["containment_closed"] = True

    class Stream:
        def read(self, _size: int) -> bytes:
            return b""

        def close(self) -> None:
            return None

    class Process:
        stdout = Stream()
        stderr = Stream()

        def poll(self) -> int:
            return 0

        def wait(self, *, timeout: float | None = None) -> int:
            captured["timeout"] = timeout
            return 0

        def kill(self) -> None:
            raise AssertionError("completed process must not be killed")

    def fake_popen(command: list[str], **kwargs: object) -> Process:
        captured["command"] = command
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runtime, "_git_environment", lambda: {"LC_ALL": "C"})
    monkeypatch.setattr(runtime, "open_process_tree", Containment)

    result = runtime._run_bounded(
        ["C:/Git/git.exe", "--version"],
        stdout_limit=10,
        stderr_limit=20,
        timeout_seconds=3.0,
    )

    assert result.returncode == 0
    assert captured["command"] == ["C:/Git/git.exe", "--version"]
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE
    assert captured["env"] == {"LC_ALL": "C"}
    assert captured["shell"] is False
    assert captured["text"] is False
    assert captured["creationflags"] == 0
    assert captured["start_new_session"] is False
    assert isinstance(captured["timeout"], float)
    assert 0 <= captured["timeout"] <= 3.0
    assert captured["attached"] is True
    assert captured["containment_closed"] is True


def test_git_environment_is_fixed_and_drops_hostile_process_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_names = (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_ASKPASS",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_EXEC_PATH",
        "GIT_EXTERNAL_DIFF",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PAGER",
        "GIT_SSH_COMMAND",
        "GIT_WORK_TREE",
        "HOME",
        "LD_PRELOAD",
        "PAGER",
        "PATH",
        "PYTHONPATH",
        "XDG_CONFIG_HOME",
    )
    for name in hostile_names:
        monkeypatch.setenv(name, "hostile")
    monkeypatch.setenv("SYSTEMROOT", "system-root")

    environment = runtime._git_environment()

    expected_names = {
        "GCM_INTERACTIVE",
        "GIT_ATTR_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_SYSTEM",
        "GIT_NO_LAZY_FETCH",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OPTIONAL_LOCKS",
        "GIT_TERMINAL_PROMPT",
        "LC_ALL",
    }
    expected_names.update(runtime._PROCESS_ENVIRONMENT_ALLOWLIST & runtime.os.environ.keys())
    assert set(environment) == expected_names
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_git_errors_are_terminal_safe_bounded_and_single_line() -> None:
    detail = safe_git_error(("bad\u202e ref\n::error::" + "x" * 5000).encode())

    assert "\n" not in detail
    assert "\u202e" not in detail
    assert len(detail) <= 1000


def test_resolves_normal_and_linked_worktree_common_directories(
    repository: Path,
    tmp_path: Path,
) -> None:
    normal = open_repository(repository)
    assert resolve_common_git_directory(normal) == (repository / ".git").resolve()

    linked = tmp_path.with_name(f"{tmp_path.name}-common-linked")
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "--quiet", "--detach", str(linked)],
        check=True,
        capture_output=True,
    )
    assert resolve_common_git_directory(open_repository(linked)) == (repository / ".git").resolve()


@pytest.mark.parametrize("output", [b"", b"one\ntwo\n", b"bad\0path\n", b"\xff\n"])
def test_common_directory_rejects_malformed_git_output(
    output: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        runtime,
        "run_git",
        lambda *_args, **_kwargs: ProcessResult(0, output, b"", False, False, False),
    )

    with pytest.raises(GitError, match="malformed common history metadata"):
        resolve_common_git_directory(GitRepository(tmp_path.resolve(), sys.executable))


def test_complete_history_rejects_shallow_repositories_and_legacy_grafts(
    repository: Path,
    tmp_path: Path,
) -> None:
    selected = open_repository(repository)
    assert (
        require_complete_history(
            selected,
            shallow_message="selection requires complete non-shallow history",
            grafts_message="selection rejects grafts",
        )
        == (repository / ".git").resolve()
    )

    grafts = repository / ".git" / "info" / "grafts"
    grafts.write_text("a" * 40 + "\n", encoding="ascii")
    with pytest.raises(GitError, match="selection rejects grafts"):
        require_complete_history(
            selected,
            shallow_message="selection requires complete non-shallow history",
            grafts_message="selection rejects grafts",
        )
    grafts.unlink()

    shallow = tmp_path.with_name(f"{tmp_path.name}-shallow")
    subprocess.run(
        ["git", "clone", "--quiet", "--depth=1", "--no-local", str(repository), str(shallow)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(GitError, match="selection requires complete non-shallow history"):
        require_complete_history(
            open_repository(shallow),
            shallow_message="selection requires complete non-shallow history",
            grafts_message="selection rejects grafts",
        )


def test_complete_history_requires_regular_grafts_metadata(repository: Path) -> None:
    grafts = repository / ".git" / "info" / "grafts"
    grafts.mkdir()

    with pytest.raises(GitError, match="overlay metadata cannot be validated safely"):
        require_complete_history(
            open_repository(repository),
            shallow_message="selection requires complete non-shallow history",
            grafts_message="selection rejects grafts",
        )


def test_replacement_refs_are_disabled_by_the_shared_runtime(repository: Path) -> None:
    original = git(repository, "rev-parse", "HEAD")
    (repository / "other.txt").write_text("other\n", encoding="utf-8")
    git(repository, "add", "other.txt")
    git(repository, "commit", "--quiet", "-m", "feat: add other")
    replacement = git(repository, "rev-parse", "HEAD")
    git(repository, "replace", original, replacement)
    assert git(repository, "rev-parse", f"{original}^{{tree}}") == git(
        repository, "rev-parse", f"{replacement}^{{tree}}"
    )

    result = run_git(
        open_repository(repository),
        ["rev-parse", "--verify", f"{original}^{{tree}}"],
        stdout_limit=256,
    )

    assert parse_object_id(result.stdout, label="tree") != git(
        repository, "rev-parse", f"{replacement}^{{tree}}"
    )
