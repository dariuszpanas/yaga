"""Bounded Docker and subprocess runtime for the pinned actionlint adapter."""

from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

from yaga.errors import InputError, safe_error_text

ACTIONLINT_IMAGE = (
    "rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
)
ACTIONLINT_FORMAT = "{{json .}}"
MAX_ACTIONLINT_SECONDS = 30.0
MAX_ACTIONLINT_STDOUT_BYTES = 2 * 1024 * 1024
MAX_ACTIONLINT_STDERR_BYTES = 64 * 1024
MAX_DOCKER_CONTROL_STDOUT_BYTES = 1024
MAX_CONTAINER_CLEANUP_SECONDS = 10.0
ACTIONLINT_MEMORY = "256m"
ACTIONLINT_CPUS = "1"
_STREAM_CHUNK_BYTES = 64 * 1024
_PROCESS_KILL_SECONDS = 5.0
_CONTAINER_LABEL = "io.yaga.actionlint.run"
_CONTAINER_NAME_PREFIX = "yaga-actionlint-"
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_overflow: bool
    stderr_overflow: bool
    timed_out: bool


class DockerRuntime(str):
    """An absolute Docker executable carrying its vetted subprocess environment."""

    environment: Mapping[str, str]

    def __new__(cls, executable: str, environment: Mapping[str, str]) -> DockerRuntime:
        instance = super().__new__(cls, executable)
        instance.environment = dict(environment)
        return instance


class _ProcessTree:
    """One bounded subprocess tree with platform-native descendant cleanup."""

    def __init__(self, process: subprocess.Popen[bytes], windows_job: object | None) -> None:
        self.process = process
        self.windows_job = windows_job
        self.closed = False
        self.cleanup_error: OSError | None = None

    def _remember_cleanup_error(self, error: OSError) -> None:
        if self.cleanup_error is None:
            self.cleanup_error = error

    def terminate(self) -> None:
        if self.closed:
            return
        self.closed = True
        if os.name == "nt" and self.windows_job is not None:
            try:
                _close_windows_handle(self.windows_job)
            except OSError as error:
                self._remember_cleanup_error(error)
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as error:
                # A direct-PID fallback cannot prove descendants were terminated.
                self._remember_cleanup_error(error)
        if self.process.poll() is None:
            try:
                self.process.kill()
            except OSError as error:
                self._remember_cleanup_error(error)


def find_docker(repository: Path) -> str | None:
    """Find Docker only through absolute PATH entries outside the untrusted repository."""
    executable_names = ("docker.exe",) if os.name == "nt" else ("docker",)
    for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = os.path.expandvars(raw_entry.strip().strip('"'))
        if not entry:
            continue
        try:
            directory = Path(entry).expanduser()
        except (OSError, RuntimeError):
            continue
        if not directory.is_absolute():
            continue
        for executable_name in executable_names:
            candidate = directory / executable_name
            try:
                resolved = candidate.resolve(strict=True)
                is_file = resolved.is_file()
            except (OSError, RuntimeError):
                continue
            if not is_file or resolved.is_relative_to(repository):
                continue
            if os.name != "nt" and not os.access(resolved, os.X_OK):
                continue
            return DockerRuntime(
                str(resolved),
                _vetted_docker_environment(resolved, repository),
            )
    return None


def run_docker_control_process(
    command: list[str],
    *,
    content: bytes,
    cwd: Path,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
) -> ProcessResult:
    try:
        return run_bounded_process(
            command,
            content=content,
            cwd=cwd,
            timeout=timeout,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            environment=_docker_environment(command[0]),
        )
    except OSError as error:
        raise InputError("Docker control process could not be executed") from error


def create_workspace_volume(
    docker: str,
    runtime_directory: Path,
    volume_name: str,
    token: str,
    *,
    timeout: float,
) -> None:
    command = [
        docker,
        "volume",
        "create",
        "--label",
        f"{_CONTAINER_LABEL}={token}",
        volume_name,
    ]
    try:
        result = run_docker_control_process(
            command,
            content=b"",
            cwd=runtime_directory,
            timeout=min(MAX_ACTIONLINT_SECONDS, timeout),
            stdout_limit=MAX_DOCKER_CONTROL_STDOUT_BYTES,
            stderr_limit=MAX_ACTIONLINT_STDERR_BYTES,
        )
        _require_docker_control_result(result, operation="create the actionlint workspace")
        if _single_line(result.stdout) != volume_name:
            raise InputError("Docker returned an unexpected actionlint workspace name")
    except BaseException:
        # Any failed create is uncertain: the daemon may finish after the client fails.
        remove_workspace_volume(
            docker,
            runtime_directory,
            volume_name,
            token,
            wait_for_late=True,
        )
        raise


def stage_workspace_snapshot(
    docker: str,
    runtime_directory: Path,
    volume_name: str,
    snapshot_archive: bytes,
    *,
    timeout: float,
) -> None:
    token = secrets.token_hex(16)
    container_name = f"{_CONTAINER_NAME_PREFIX}stage-{token}"
    command = [
        docker,
        "create",
        "--name",
        container_name,
        "--label",
        f"{_CONTAINER_LABEL}={token}",
        "--network",
        "none",
        "--read-only",
        "--log-driver",
        "none",
        "--mount",
        f"type=volume,source={volume_name},target=/workspace,volume-nocopy",
        "--workdir",
        "/workspace",
        ACTIONLINT_IMAGE,
        "-version",
    ]
    deadline = time.monotonic() + min(MAX_ACTIONLINT_SECONDS, timeout)
    try:
        container_id = create_labeled_container(
            docker,
            runtime_directory,
            token,
            container_name,
            command,
            timeout=_remaining_deadline(deadline, operation="stage the actionlint workspace"),
        )
        copy_result = run_docker_control_process(
            [docker, "cp", "-", f"{container_id}:/workspace"],
            content=snapshot_archive,
            cwd=runtime_directory,
            timeout=_remaining_deadline(deadline, operation="stage the actionlint workspace"),
            stdout_limit=MAX_DOCKER_CONTROL_STDOUT_BYTES,
            stderr_limit=MAX_ACTIONLINT_STDERR_BYTES,
        )
        _require_docker_control_result(copy_result, operation="stage the actionlint workspace")
    finally:
        remove_labeled_containers(
            docker,
            runtime_directory,
            token,
            container_name=container_name,
        )


def run_actionlint_container(
    docker: str,
    runtime_directory: Path,
    volume_name: str,
    relative_path: str,
    *,
    content: bytes,
    config_path: str | None,
    timeout: float,
) -> ProcessResult:
    token = secrets.token_hex(16)
    container_name = f"{_CONTAINER_NAME_PREFIX}lint-{token}"
    actionlint_arguments = [
        "-no-color",
        "-format",
        ACTIONLINT_FORMAT,
    ]
    if config_path is not None:
        actionlint_arguments.extend(["-config-file", config_path])
    actionlint_arguments.extend(["-stdin-filename", relative_path, "-"])
    command = [
        docker,
        "create",
        "--name",
        container_name,
        "--label",
        f"{_CONTAINER_LABEL}={token}",
        "--interactive",
        "--network",
        "none",
        "--read-only",
        "--log-driver",
        "none",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        ACTIONLINT_MEMORY,
        "--memory-swap",
        ACTIONLINT_MEMORY,
        "--cpus",
        ACTIONLINT_CPUS,
        "--mount",
        (f"type=volume,source={volume_name},target=/workspace,readonly,volume-nocopy"),
        "--workdir",
        "/workspace",
        ACTIONLINT_IMAGE,
        *actionlint_arguments,
    ]
    deadline = time.monotonic() + timeout
    try:
        container_id = create_labeled_container(
            docker,
            runtime_directory,
            token,
            container_name,
            command,
            timeout=_remaining_deadline(deadline, operation="create the actionlint container"),
        )
        return run_bounded_process(
            [docker, "start", "--attach", "--interactive", container_id],
            content=content,
            cwd=runtime_directory,
            timeout=_remaining_deadline(deadline, operation="run actionlint"),
            stdout_limit=MAX_ACTIONLINT_STDOUT_BYTES,
            stderr_limit=MAX_ACTIONLINT_STDERR_BYTES,
            environment=_docker_environment(docker),
        )
    finally:
        remove_labeled_containers(
            docker,
            runtime_directory,
            token,
            container_name=container_name,
        )


def create_labeled_container(
    docker: str,
    runtime_directory: Path,
    token: str,
    container_name: str,
    command: list[str],
    *,
    timeout: float,
) -> str:
    try:
        result = run_docker_control_process(
            command,
            content=b"",
            cwd=runtime_directory,
            timeout=timeout,
            stdout_limit=MAX_DOCKER_CONTROL_STDOUT_BYTES,
            stderr_limit=MAX_ACTIONLINT_STDERR_BYTES,
        )
        _require_docker_control_result(result, operation="create an actionlint container")
        container_id = _single_line(result.stdout)
        if _CONTAINER_ID.fullmatch(container_id) is None:
            raise InputError("Docker returned a malformed actionlint container ID")
        return container_id
    except BaseException:
        # Any failed create is uncertain: remove by exact name and poll the private label.
        remove_labeled_containers(
            docker,
            runtime_directory,
            token,
            container_name=container_name,
            wait_for_late=True,
        )
        raise


def _require_docker_control_result(result: ProcessResult, *, operation: str) -> None:
    if result.timed_out:
        raise InputError(f"Docker timed out while attempting to {operation}")
    if result.stdout_overflow or result.stderr_overflow:
        raise InputError(f"Docker exceeded its output limit while attempting to {operation}")
    if result.returncode != 0:
        detail = safe_process_error(result.stderr)
        raise InputError(f"Docker could not {operation}: {detail}")


def _single_line(value: bytes) -> str:
    try:
        text = value.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise InputError("Docker returned non-ASCII control output") from error
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not text or "\n" in text or "\r" in text:
        raise InputError("Docker returned malformed control output")
    return text


def _remaining_deadline(deadline: float, *, operation: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InputError(f"Docker timed out while attempting to {operation}")
    return remaining


def remove_labeled_containers(
    docker: str,
    runtime_directory: Path,
    token: str,
    *,
    container_name: str | None = None,
    wait_for_late: bool = False,
) -> None:
    resource = container_name or f"label-{token}"
    try:
        started = time.monotonic()
        late_deadline = started + MAX_CONTAINER_CLEANUP_SECONDS if wait_for_late else started
        deadline = late_deadline + MAX_CONTAINER_CLEANUP_SECONDS
        if container_name is not None:
            try:
                run_docker_control_process(
                    [docker, "container", "rm", "--force", container_name],
                    content=b"",
                    cwd=runtime_directory,
                    timeout=_remaining_cleanup_time(deadline),
                    stdout_limit=MAX_DOCKER_CONTROL_STDOUT_BYTES,
                    stderr_limit=MAX_ACTIONLINT_STDERR_BYTES,
                )
            except InputError:
                # Not-found and uncertain client failures are resolved by label polling below.
                pass
        while True:
            identifiers = _list_labeled_containers(
                docker,
                runtime_directory,
                token,
                deadline=deadline,
            )
            for identifier in identifiers:
                try:
                    run_docker_control_process(
                        [docker, "container", "rm", "--force", identifier],
                        content=b"",
                        cwd=runtime_directory,
                        timeout=_remaining_cleanup_time(deadline),
                        stdout_limit=MAX_DOCKER_CONTROL_STDOUT_BYTES,
                        stderr_limit=MAX_ACTIONLINT_STDERR_BYTES,
                    )
                except InputError:
                    pass
            remaining = _list_labeled_containers(
                docker,
                runtime_directory,
                token,
                deadline=deadline,
            )
            if remaining:
                if time.monotonic() >= deadline:
                    raise InputError("container remained after removal attempts")
                _pause_cleanup_poll(deadline)
                continue
            if time.monotonic() >= late_deadline:
                return
            _pause_cleanup_poll(late_deadline)
    except InputError as error:
        detail = safe_error_text(error, maximum=200)
        message = f"Docker could not remove actionlint container {resource}"
        if detail:
            message += f": {detail}"
        raise InputError(message) from error


def _list_labeled_containers(
    docker: str,
    runtime_directory: Path,
    token: str,
    *,
    deadline: float,
) -> tuple[str, ...]:
    result = run_docker_control_process(
        [
            docker,
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label={_CONTAINER_LABEL}={token}",
        ],
        content=b"",
        cwd=runtime_directory,
        timeout=_remaining_cleanup_time(deadline),
        stdout_limit=MAX_DOCKER_CONTROL_STDOUT_BYTES,
        stderr_limit=MAX_ACTIONLINT_STDERR_BYTES,
    )
    _require_docker_control_result(result, operation="inspect actionlint containers")
    identifiers = _control_output_lines(result.stdout)
    if len(identifiers) > 1 or any(_CONTAINER_ID.fullmatch(item) is None for item in identifiers):
        raise InputError("Docker returned malformed actionlint container state")
    return identifiers


def remove_workspace_volume(
    docker: str,
    runtime_directory: Path,
    volume_name: str,
    token: str,
    *,
    wait_for_late: bool = False,
) -> None:
    try:
        started = time.monotonic()
        late_deadline = started + MAX_CONTAINER_CLEANUP_SECONDS if wait_for_late else started
        deadline = late_deadline + MAX_CONTAINER_CLEANUP_SECONDS
        try:
            run_docker_control_process(
                [docker, "volume", "rm", "--force", volume_name],
                content=b"",
                cwd=runtime_directory,
                timeout=_remaining_cleanup_time(deadline),
                stdout_limit=MAX_DOCKER_CONTROL_STDOUT_BYTES,
                stderr_limit=MAX_ACTIONLINT_STDERR_BYTES,
            )
        except InputError:
            # Not-found and uncertain client failures are resolved by label polling below.
            pass
        while True:
            volumes = _list_labeled_volumes(
                docker,
                runtime_directory,
                token,
                deadline=deadline,
            )
            if volume_name in volumes:
                try:
                    run_docker_control_process(
                        [docker, "volume", "rm", "--force", volume_name],
                        content=b"",
                        cwd=runtime_directory,
                        timeout=_remaining_cleanup_time(deadline),
                        stdout_limit=MAX_DOCKER_CONTROL_STDOUT_BYTES,
                        stderr_limit=MAX_ACTIONLINT_STDERR_BYTES,
                    )
                except InputError:
                    pass
            remaining = _list_labeled_volumes(
                docker,
                runtime_directory,
                token,
                deadline=deadline,
            )
            if remaining:
                if time.monotonic() >= deadline:
                    raise InputError("workspace remained after removal attempts")
                _pause_cleanup_poll(deadline)
                continue
            if time.monotonic() >= late_deadline:
                return
            _pause_cleanup_poll(late_deadline)
    except InputError as error:
        detail = safe_error_text(error, maximum=200)
        message = f"Docker could not remove private actionlint workspace {volume_name}"
        if detail:
            message += f": {detail}"
        raise InputError(message) from error


def _list_labeled_volumes(
    docker: str,
    runtime_directory: Path,
    token: str,
    *,
    deadline: float,
) -> tuple[str, ...]:
    result = run_docker_control_process(
        [
            docker,
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label={_CONTAINER_LABEL}={token}",
        ],
        content=b"",
        cwd=runtime_directory,
        timeout=_remaining_cleanup_time(deadline),
        stdout_limit=MAX_DOCKER_CONTROL_STDOUT_BYTES,
        stderr_limit=MAX_ACTIONLINT_STDERR_BYTES,
    )
    _require_docker_control_result(result, operation="inspect the actionlint workspace")
    volumes = _control_output_lines(result.stdout)
    if len(volumes) > 1:
        raise InputError("Docker returned malformed actionlint workspace state")
    return volumes


def _control_output_lines(value: bytes) -> tuple[str, ...]:
    try:
        text = value.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise InputError("Docker returned non-ASCII control output") from error
    lines = tuple(line for line in text.splitlines() if line)
    if any(any(character.isspace() for character in line) for line in lines):
        raise InputError("Docker returned malformed control output")
    return lines


def _remaining_cleanup_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InputError("Docker actionlint cleanup exceeded its hard timeout")
    return remaining


def _pause_cleanup_poll(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    time.sleep(min(0.1, remaining))


def safe_process_error(value: bytes) -> str:
    try:
        decoded = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "the Docker runtime returned non-UTF-8 error output"
    return safe_error_text(decoded, maximum=200) or "the Docker runtime failed"


def _vetted_docker_environment(docker: Path, repository: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PATH"] = str(docker.parent)
    raw_config = environment.get("DOCKER_CONFIG")
    if raw_config is None:
        return environment
    try:
        expanded = os.path.expandvars(raw_config.strip().strip('"'))
        config = Path(expanded).expanduser()
        if not config.is_absolute():
            raise ValueError
        resolved = config.resolve(strict=False)
        if resolved.is_relative_to(repository):
            raise ValueError
    except (OSError, RuntimeError, ValueError) as error:
        raise InputError("DOCKER_CONFIG must resolve outside the repository") from error
    environment["DOCKER_CONFIG"] = str(resolved)
    return environment


def _docker_environment(docker: str) -> dict[str, str]:
    """Return a Docker environment without repository-controlled helper resolution."""
    if isinstance(docker, DockerRuntime):
        return dict(docker.environment)
    environment = dict(os.environ)
    executable = Path(docker)
    if executable.is_absolute():
        try:
            environment["PATH"] = str(executable.parent.resolve(strict=True))
        except (OSError, RuntimeError):
            environment["PATH"] = str(executable.parent)
    else:
        # Production discovery always returns an absolute executable. Keeping an empty
        # fallback prevents a direct internal call from searching an untrusted PATH.
        environment["PATH"] = ""
    environment.pop("DOCKER_CONFIG", None)
    return environment


def _create_windows_job() -> object:
    import ctypes
    from ctypes import wintypes

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_int64),
            ("per_job_user_time_limit", ctypes.c_int64),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_uint64),
            ("write_operation_count", ctypes.c_uint64),
            ("other_operation_count", ctypes.c_uint64),
            ("read_transfer_count", ctypes.c_uint64),
            ("write_transfer_count", ctypes.c_uint64),
            ("other_transfer_count", ctypes.c_uint64),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", _BasicLimitInformation),
            ("io_info", _IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    # These attributes intentionally exist only on Windows. Dynamic lookup keeps
    # cross-platform type checking from treating the guarded implementation as invalid.
    windows_ctypes = cast(Any, ctypes)
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    get_last_error = windows_ctypes.get_last_error
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(get_last_error(), "could not create a subprocess job")
    information = _ExtendedLimitInformation()
    information.basic_limit_information.limit_flags = 0x00002000
    if not kernel32.SetInformationJobObject(
        handle,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, "could not configure a subprocess job")
    return handle


def _assign_windows_job(handle: object, process: subprocess.Popen[bytes]) -> None:
    import ctypes
    from ctypes import wintypes

    windows_ctypes = cast(Any, ctypes)
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    get_last_error = windows_ctypes.get_last_error
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    raw_handle = vars(process).get("_handle")
    if raw_handle is None:
        raise OSError("subprocess handle is unavailable")
    process_handle = wintypes.HANDLE(int(cast(Any, raw_handle)))
    if not kernel32.AssignProcessToJobObject(handle, process_handle):
        raise OSError(get_last_error(), "could not assign a subprocess job")


def _close_windows_handle(handle: object) -> None:
    import ctypes
    from ctypes import wintypes

    windows_ctypes = cast(Any, ctypes)
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    get_last_error = windows_ctypes.get_last_error
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(handle):
        raise OSError(get_last_error(), "could not close a subprocess job")


def _spawn_bounded_process(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None,
) -> _ProcessTree:
    windows_job = _create_windows_job() if os.name == "nt" else None
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name != "nt",
            env=environment,
        )
    except BaseException:
        if windows_job is not None:
            _close_windows_handle(windows_job)
        raise
    try:
        if windows_job is not None:
            _assign_windows_job(windows_job, process)
    except BaseException:
        cleanup_error: BaseException | None = None
        try:
            process.kill()
            process.wait(timeout=_PROCESS_KILL_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            cleanup_error = error
        try:
            _close_windows_handle(windows_job)
        except OSError as error:
            if cleanup_error is None:
                cleanup_error = error
        if cleanup_error is not None:
            raise OSError("could not clean up an unassigned subprocess") from cleanup_error
        raise
    return _ProcessTree(process, windows_job)


def _raise_process_cleanup_error(process_tree: _ProcessTree) -> None:
    if process_tree.cleanup_error is not None:
        raise OSError(
            "could not terminate the bounded subprocess tree"
        ) from process_tree.cleanup_error


def run_bounded_process(
    command: list[str],
    *,
    content: bytes,
    cwd: Path,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    environment: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Run a process with bounded input time and streaming pipe capture."""
    if timeout <= 0 or stdout_limit < 1 or stderr_limit < 1:
        raise ValueError("process timeout and output limits must be positive")
    process_tree = _spawn_bounded_process(command, cwd=cwd, environment=environment)
    process = process_tree.process
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    stdout = bytearray()
    stderr = bytearray()
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    pipe_errors: list[OSError] = []
    kill_lock = threading.Lock()

    def kill_process() -> None:
        with kill_lock:
            process_tree.terminate()

    threads = [
        threading.Thread(
            target=_write_stream,
            args=(process.stdin, content, kill_process, pipe_errors),
            daemon=True,
        ),
        threading.Thread(
            target=_capture_stream,
            args=(
                process.stdout,
                stdout_limit,
                stdout,
                stdout_overflow,
                kill_process,
                pipe_errors,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=_capture_stream,
            args=(
                process.stderr,
                stderr_limit,
                stderr,
                stderr_overflow,
                kill_process,
                pipe_errors,
            ),
            daemon=True,
        ),
    ]
    started_threads: list[threading.Thread] = []
    timed_out = False
    try:
        for thread in threads:
            thread.start()
            started_threads.append(thread)
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process()
            try:
                returncode = process.wait(timeout=_PROCESS_KILL_SECONDS)
            except subprocess.TimeoutExpired as error:
                raise OSError("process did not stop after termination") from error
        kill_process()
    except BaseException:
        kill_process()
        try:
            process.wait(timeout=_PROCESS_KILL_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _join_process_threads(process, started_threads)
        _raise_process_cleanup_error(process_tree)
        raise

    if not _join_process_threads(process, started_threads):
        kill_process()
        _raise_process_cleanup_error(process_tree)
        raise OSError("process pipe did not close")
    _raise_process_cleanup_error(process_tree)
    if pipe_errors and not (timed_out or stdout_overflow.is_set() or stderr_overflow.is_set()):
        raise OSError("could not transfer process input or output") from pipe_errors[0]

    return ProcessResult(
        returncode=-1 if timed_out and returncode == 0 else returncode,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
        stdout_overflow=stdout_overflow.is_set(),
        stderr_overflow=stderr_overflow.is_set(),
        timed_out=timed_out,
    )


def _join_process_threads(
    process: subprocess.Popen[bytes],
    threads: Sequence[threading.Thread],
) -> bool:
    deadline = time.monotonic() + _PROCESS_KILL_SECONDS
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    for thread in threads:
        if thread.is_alive():
            thread.join(timeout=0.1)
    return not any(thread.is_alive() for thread in threads)


def _write_stream(
    stream: IO[bytes],
    content: bytes,
    kill_process: Callable[[], None],
    pipe_errors: list[OSError],
) -> None:
    try:
        offset = 0
        while offset < len(content):
            written = stream.write(content[offset : offset + _STREAM_CHUNK_BYTES])
            if written is None or written <= 0:
                raise OSError("process input pipe stopped accepting bytes")
            offset += written
        stream.flush()
    except BrokenPipeError:
        pass
    except OSError as error:
        pipe_errors.append(error)
        kill_process()
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _capture_stream(
    stream: IO[bytes],
    limit: int,
    destination: bytearray,
    overflow: threading.Event,
    kill_process: Callable[[], None],
    pipe_errors: list[OSError],
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
        pipe_errors.append(error)
        kill_process()
    finally:
        try:
            stream.close()
        except OSError:
            pass
