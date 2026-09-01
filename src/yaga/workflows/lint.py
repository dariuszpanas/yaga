"""Orchestration and strict diagnostics for the pinned actionlint adapter."""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import FrameType

from yaga.errors import InputError, YagaError, safe_error_text
from yaga.workflows import actionlint_runtime as _runtime
from yaga.workflows import actionlint_snapshot as _snapshot
from yaga.workflows.inputs import MAX_WORKFLOW_BYTES, WorkflowInput, load_workflow_inputs
from yaga.workflows.models import WorkflowDiagnostic, WorkflowLintReport, WorkflowLintResult

# Transitional private aliases keep the focused low-level tests readable without making
# snapshot or Docker lifecycle code part of this orchestration module again.
ACTIONLINT_CPUS = _runtime.ACTIONLINT_CPUS
ACTIONLINT_FORMAT = _runtime.ACTIONLINT_FORMAT
ACTIONLINT_IMAGE = _runtime.ACTIONLINT_IMAGE
ACTIONLINT_MEMORY = _runtime.ACTIONLINT_MEMORY
MAX_ACTIONLINT_SECONDS = _runtime.MAX_ACTIONLINT_SECONDS
MAX_ACTIONLINT_STDERR_BYTES = _runtime.MAX_ACTIONLINT_STDERR_BYTES
MAX_ACTIONLINT_STDOUT_BYTES = _runtime.MAX_ACTIONLINT_STDOUT_BYTES
MAX_CONTAINER_CLEANUP_SECONDS = _runtime.MAX_CONTAINER_CLEANUP_SECONDS
MAX_ACTIONLINT_ARCHIVE_BYTES = _snapshot.MAX_ACTIONLINT_ARCHIVE_BYTES
MAX_ACTIONLINT_ARCHIVE_ENTRIES = _snapshot.MAX_ACTIONLINT_ARCHIVE_ENTRIES
MAX_ACTIONLINT_PATH_BYTES = _snapshot.MAX_ACTIONLINT_PATH_BYTES
MAX_ACTIONLINT_PATH_COMPONENTS = _snapshot.MAX_ACTIONLINT_PATH_COMPONENTS
MAX_ACTIONLINT_SNAPSHOT_BYTES = _snapshot.MAX_ACTIONLINT_SNAPSHOT_BYTES
MAX_ACTIONLINT_SNAPSHOT_FILES = _snapshot.MAX_ACTIONLINT_SNAPSHOT_FILES
MAX_ACTIONLINT_SNAPSHOT_NODES = _snapshot.MAX_ACTIONLINT_SNAPSHOT_NODES
MAX_ACTIONLINT_SNAPSHOT_REFERENCES = _snapshot.MAX_ACTIONLINT_SNAPSHOT_REFERENCES
_ACTIONLINT_CONFIG_PATHS = _snapshot.ACTIONLINT_CONFIG_PATHS
_ProcessResult = _runtime.ProcessResult
_action_runtime_path = _snapshot.action_runtime_path
_build_actionlint_archive = _snapshot.build_actionlint_archive
_build_actionlint_snapshot = _snapshot.build_actionlint_snapshot
_create_labeled_container = _runtime.create_labeled_container
_create_workspace_volume = _runtime.create_workspace_volume
_find_docker = _runtime.find_docker
_normalize_self_references = _snapshot.normalize_self_references
_remove_labeled_containers = _runtime.remove_labeled_containers
_remove_workspace_volume = _runtime.remove_workspace_volume
_run_actionlint_container = _runtime.run_actionlint_container
_run_bounded_process = _runtime.run_bounded_process
_run_docker_control_process = _runtime.run_docker_control_process
_safe_process_error = _runtime.safe_process_error
_stage_workspace_snapshot = _runtime.stage_workspace_snapshot
_validate_snapshot_relative_path = _snapshot.validate_snapshot_relative_path

__all__ = [
    "ACTIONLINT_CPUS",
    "ACTIONLINT_FORMAT",
    "ACTIONLINT_IMAGE",
    "ACTIONLINT_MEMORY",
    "MAX_ACTIONLINT_ARCHIVE_BYTES",
    "MAX_ACTIONLINT_ARCHIVE_ENTRIES",
    "MAX_ACTIONLINT_PATH_BYTES",
    "MAX_ACTIONLINT_PATH_COMPONENTS",
    "MAX_ACTIONLINT_SNAPSHOT_BYTES",
    "MAX_ACTIONLINT_SNAPSHOT_FILES",
    "MAX_ACTIONLINT_SNAPSHOT_NODES",
    "MAX_ACTIONLINT_SNAPSHOT_REFERENCES",
    "MAX_CONTAINER_CLEANUP_SECONDS",
    "MAX_WORKFLOW_BYTES",
    "_ProcessResult",
    "_action_runtime_path",
    "_build_actionlint_archive",
    "_build_actionlint_snapshot",
    "_create_labeled_container",
    "_normalize_self_references",
    "_remove_labeled_containers",
    "_run_bounded_process",
    "_run_docker_control_process",
    "_validate_snapshot_relative_path",
    "lint_workflows",
]

MAX_TOTAL_ACTIONLINT_SECONDS = 300.0
MAX_ACTIONLINT_DIAGNOSTICS = 512
MAX_ACTIONLINT_MESSAGE_CHARS = 500
MAX_ACTIONLINT_KIND_CHARS = 64
MAX_ACTIONLINT_POSITION = 1024 * 1024 + 1
_ACTIONLINT_DIAGNOSTIC_REQUIRED_FIELDS = frozenset(
    {"message", "filepath", "line", "column", "kind"}
)
_ACTIONLINT_DIAGNOSTIC_OPTIONAL_FIELDS = frozenset({"snippet", "end_column"})
_ACTIONLINT_DIAGNOSTIC_FIELDS = (
    _ACTIONLINT_DIAGNOSTIC_REQUIRED_FIELDS | _ACTIONLINT_DIAGNOSTIC_OPTIONAL_FIELDS
)
_CONTAINER_NAME_PREFIX = "yaga-actionlint-"
_KIND_CHARACTER = re.compile(r"[^a-z0-9]+")


def lint_workflows(
    repository: Path,
    selections: Sequence[Path] = (),
) -> WorkflowLintReport:
    """Lint selected workflows with the immutable, isolated actionlint image."""
    workflows = load_workflow_inputs(repository, selections)
    repo = _repository_from_workflow(workflows[0].path, workflows[0].relative_path)
    docker = _find_docker(repo)
    if docker is None:
        raise InputError("Docker is required for workflow linting")

    try:
        with tempfile.TemporaryDirectory(prefix="yaga-actionlint-") as temporary_directory:
            return _lint_in_temporary_workspace(
                repo,
                workflows,
                docker,
                Path(temporary_directory).resolve(),
            )
    except YagaError:
        raise
    except (OSError, RuntimeError) as error:
        raise InputError("actionlint temporary workspace could not be managed safely") from error


def _lint_in_temporary_workspace(
    repo: Path,
    workflows: tuple[WorkflowInput, ...],
    docker: str,
    runtime_directory: Path,
) -> WorkflowLintReport:
    deadline = time.monotonic() + MAX_TOTAL_ACTIONLINT_SECONDS
    total_diagnostics = 0
    results: list[WorkflowLintResult] = []
    snapshot = _build_actionlint_snapshot(repo, workflows)
    config_path = next((path for path in _ACTIONLINT_CONFIG_PATHS if path in snapshot), None)
    snapshot_archive = _build_actionlint_archive(snapshot)
    volume_token = secrets.token_hex(16)
    volume_name = f"{_CONTAINER_NAME_PREFIX}{volume_token}"

    with _cleanup_on_termination():
        # The cleanup region begins before create so asynchronous exceptions cannot strand a
        # successfully-created private volume between the create return and a later try block.
        try:
            _create_workspace_volume(
                docker,
                runtime_directory,
                volume_name,
                volume_token,
                timeout=_remaining_actionlint_time(deadline),
            )
            _stage_workspace_snapshot(
                docker,
                runtime_directory,
                volume_name,
                snapshot_archive,
                timeout=_remaining_actionlint_time(deadline),
            )
            for workflow in workflows:
                remaining = _remaining_actionlint_time(deadline)
                result = _lint_workflow(
                    docker,
                    runtime_directory,
                    volume_name,
                    workflow.relative_path,
                    snapshot[workflow.relative_path],
                    config_path=config_path,
                    timeout=min(MAX_ACTIONLINT_SECONDS, remaining),
                )
                total_diagnostics += len(result.diagnostics)
                if total_diagnostics > MAX_ACTIONLINT_DIAGNOSTICS:
                    raise InputError(
                        "actionlint findings exceed the hard "
                        f"{MAX_ACTIONLINT_DIAGNOSTICS}-diagnostic total limit"
                    )
                results.append(result)
        finally:
            _remove_workspace_volume(
                docker,
                runtime_directory,
                volume_name,
                volume_token,
            )

    return WorkflowLintReport(results=tuple(results))


@contextmanager
def _cleanup_on_termination() -> Iterator[None]:
    """Handle catchable POSIX termination and Windows console-break requests."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    selected_signals = [int(signal.SIGTERM)] if os.name != "nt" else []
    sigbreak = getattr(signal, "SIGBREAK", None)
    if isinstance(sigbreak, int):
        selected_signals.append(sigbreak)
    previous_handlers = []
    terminating = False

    def request_exit(signum: int, _frame: FrameType | None) -> None:
        nonlocal terminating
        if terminating:
            return
        terminating = True
        raise SystemExit(128 + signum)

    try:
        for selected_signal in selected_signals:
            previous = signal.getsignal(selected_signal)
            if previous == signal.SIG_IGN:
                continue
            signal.signal(selected_signal, request_exit)
            previous_handlers.append((selected_signal, previous))
        yield
    finally:
        for selected_signal, previous in reversed(previous_handlers):
            signal.signal(selected_signal, previous)


def _repository_from_workflow(path: Path, relative_path: str) -> Path:
    parts = PurePosixPath(relative_path).parts
    if not parts:
        raise InputError("workflow input has no repository-relative path")
    repository = path
    for _part in parts:
        repository = repository.parent
    return repository


def _remaining_actionlint_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InputError(
            f"actionlint exceeded the hard {MAX_TOTAL_ACTIONLINT_SECONDS:g}-second total timeout"
        )
    return remaining


def _lint_workflow(
    docker: str,
    runtime_directory: Path,
    volume_name: str,
    relative_path: str,
    content: bytes,
    *,
    config_path: str | None,
    timeout: float,
) -> WorkflowLintResult:
    try:
        process = _run_actionlint_container(
            docker,
            runtime_directory,
            volume_name,
            relative_path,
            content=content,
            config_path=config_path,
            timeout=timeout,
        )
    except OSError as error:
        raise InputError("could not execute the Docker actionlint runtime") from error

    display_path = safe_error_text(relative_path, maximum=200)
    if process.timed_out:
        raise InputError(f"actionlint timed out within its per-workflow limit for {display_path}")
    if process.stdout_overflow:
        raise InputError(
            "actionlint output exceeds the hard "
            f"{MAX_ACTIONLINT_STDOUT_BYTES}-byte limit for {display_path}"
        )
    if process.stderr_overflow:
        raise InputError(
            "actionlint error output exceeds the hard "
            f"{MAX_ACTIONLINT_STDERR_BYTES}-byte limit for {display_path}"
        )
    if process.returncode not in {0, 1}:
        detail = _safe_process_error(process.stderr)
        raise InputError(f"actionlint failed for {display_path}: {detail}")

    diagnostics = _parse_diagnostics(process.stdout, expected_path=relative_path)
    if (process.returncode == 0) != (not diagnostics):
        raise InputError("actionlint returned an inconsistent result")
    return WorkflowLintResult(path=relative_path, diagnostics=diagnostics)


def _parse_diagnostics(
    output: bytes,
    *,
    expected_path: str,
) -> tuple[WorkflowDiagnostic, ...]:
    try:
        document = json.loads(
            output.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_int=_bounded_json_integer,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise InputError("actionlint returned malformed JSON output") from error
    if not isinstance(document, list):
        raise InputError("actionlint JSON output must be a list")
    if len(document) > MAX_ACTIONLINT_DIAGNOSTICS:
        raise InputError(
            f"actionlint findings exceed the hard {MAX_ACTIONLINT_DIAGNOSTICS}-diagnostic limit"
        )

    diagnostics: list[WorkflowDiagnostic] = []
    for item in document:
        if not isinstance(item, Mapping):
            raise InputError("actionlint returned a malformed diagnostic")
        fields = frozenset(item)
        if (
            not _ACTIONLINT_DIAGNOSTIC_REQUIRED_FIELDS <= fields
            or not fields <= _ACTIONLINT_DIAGNOSTIC_FIELDS
        ):
            raise InputError("actionlint returned an unexpected diagnostic shape")
        filepath = item.get("filepath")
        if not isinstance(filepath, str) or filepath != expected_path:
            raise InputError("actionlint returned an unexpected diagnostic path")
        message = item.get("message")
        kind = item.get("kind")
        snippet = item.get("snippet", "")
        if (
            not isinstance(message, str)
            or not isinstance(kind, str)
            or not isinstance(snippet, str)
        ):
            raise InputError("actionlint returned a malformed diagnostic field")
        line = _diagnostic_position(item.get("line"), field="line")
        column = _diagnostic_position(item.get("column"), field="column")
        if "end_column" in item:
            _raw_diagnostic_position(item.get("end_column"), field="end_column")
        diagnostics.append(
            WorkflowDiagnostic(
                code=f"actionlint.{_safe_kind(kind)}",
                message=_safe_message(message),
                line=line,
                column=column,
            )
        )

    return tuple(
        sorted(
            diagnostics,
            key=lambda item: (item.line, item.column, item.code, item.message),
        )
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _bounded_json_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if not digits or len(digits) > len(str(MAX_ACTIONLINT_POSITION)):
        raise ValueError("JSON integer exceeds its digit limit")
    return int(value)


def _reject_json_number(_value: str) -> float:
    raise ValueError("JSON number type is not supported")


def _diagnostic_position(value: object, *, field: str) -> int:
    position = _raw_diagnostic_position(value, field=field)
    return max(1, position)


def _raw_diagnostic_position(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_ACTIONLINT_POSITION:
        raise InputError(f"actionlint returned an invalid diagnostic {field}")
    return value


def _safe_message(value: str) -> str:
    bounded = value[: MAX_ACTIONLINT_MESSAGE_CHARS * 4]
    return safe_error_text(bounded, maximum=MAX_ACTIONLINT_MESSAGE_CHARS) or "actionlint finding"


def _safe_kind(value: str) -> str:
    bounded = safe_error_text(
        value[: MAX_ACTIONLINT_KIND_CHARS * 4],
        maximum=MAX_ACTIONLINT_KIND_CHARS,
    ).lower()
    normalized = _KIND_CHARACTER.sub("-", bounded).strip("-")
    return normalized or "unknown"
