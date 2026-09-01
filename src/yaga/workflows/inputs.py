"""Bounded discovery and loading of GitHub workflow inputs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from yaga.errors import InputError, safe_error_text
from yaga.files import read_file_prefix

MAX_WORKFLOW_FILES = 128
MAX_DIRECTORY_ENTRIES = 1024
MAX_WORKFLOW_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_WORKFLOW_PATH_BYTES = 4096
MAX_WORKFLOW_PATH_COMPONENTS = 64

_WORKFLOW_SUFFIXES = frozenset({".yaml", ".yml"})
_DEFAULT_DIRECTORY = Path(".github/workflows")


@dataclass(frozen=True, slots=True)
class WorkflowInput:
    """One resolved, repository-relative, bounded workflow input."""

    path: Path
    relative_path: str
    content: bytes


def load_workflow_inputs(
    repository: Path,
    selections: Sequence[Path] = (),
) -> tuple[WorkflowInput, ...]:
    """Discover and read selected workflow files under one repository root."""
    repo = _resolve_repository(repository)
    paths = _discover_workflows(repo, selections or (_DEFAULT_DIRECTORY,))
    loaded: list[WorkflowInput] = []
    total_bytes = 0

    for path in paths:
        relative = path.relative_to(repo).as_posix()
        try:
            content = read_file_prefix(path, maximum=MAX_WORKFLOW_BYTES)
        except OSError as error:
            display_path = safe_error_text(relative, maximum=200)
            raise InputError(f"cannot read workflow {display_path}") from error

        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_BYTES:
            raise InputError(f"workflow inputs exceed the hard {MAX_TOTAL_BYTES}-byte total limit")
        if len(content) > MAX_WORKFLOW_BYTES:
            display_path = safe_error_text(relative, maximum=160) or "workflow input"
            raise InputError(f"{display_path} exceeds the hard {MAX_WORKFLOW_BYTES}-byte limit")

        loaded.append(
            WorkflowInput(
                path=path,
                relative_path=relative,
                content=content,
            )
        )

    return tuple(loaded)


def _validate_preloaded_workflow_inputs(
    workflows: tuple[WorkflowInput, ...],
    *,
    repository: Path | None = None,
) -> Path:
    """Validate the bounded, single-repository contract of an already-loaded tuple."""
    if not isinstance(workflows, tuple) or not workflows:
        raise InputError("preloaded workflow inputs must be a nonempty tuple")
    if len(workflows) > MAX_WORKFLOW_FILES:
        raise InputError(
            f"preloaded workflow inputs exceed the hard {MAX_WORKFLOW_FILES}-file limit"
        )

    expected_repository = _resolve_repository(repository) if repository is not None else None
    inferred_repository: Path | None = None
    relative_paths: set[str] = set()
    resolved_paths: set[Path] = set()
    total_bytes = 0
    for workflow in workflows:
        if not isinstance(workflow, WorkflowInput):
            raise InputError("preloaded workflow inputs contain an invalid item")
        if not isinstance(workflow.path, Path) or not isinstance(workflow.relative_path, str):
            raise InputError("preloaded workflow input has an invalid path")
        if not workflow.path.is_absolute():
            raise InputError("preloaded workflow input path must be absolute and resolved")
        if not isinstance(workflow.content, bytes):
            raise InputError("preloaded workflow input content must be bytes")

        posix_path = PurePosixPath(workflow.relative_path)
        if (
            posix_path.as_posix() != workflow.relative_path
            or "\x00" in workflow.relative_path
            or "\\" in workflow.relative_path
            or PureWindowsPath(workflow.relative_path).drive
        ):
            raise InputError(
                "preloaded workflow input path is not canonical and repository-relative"
            )
        validate_workflow_relative_path(workflow.relative_path)
        if posix_path.suffix.lower() not in _WORKFLOW_SUFFIXES:
            raise InputError("preloaded workflow files must use a .yml or .yaml extension")
        if workflow.relative_path in relative_paths:
            raise InputError("preloaded workflow inputs contain a duplicate path")
        relative_paths.add(workflow.relative_path)

        if len(workflow.content) > MAX_WORKFLOW_BYTES:
            raise InputError(
                f"preloaded workflow input exceeds the hard {MAX_WORKFLOW_BYTES}-byte limit"
            )
        total_bytes += len(workflow.content)
        if total_bytes > MAX_TOTAL_BYTES:
            raise InputError(
                f"preloaded workflow inputs exceed the hard {MAX_TOTAL_BYTES}-byte total limit"
            )

        try:
            resolved_path = workflow.path.expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise InputError("preloaded workflow input path cannot be resolved safely") from error
        if workflow.path != resolved_path:
            raise InputError("preloaded workflow input path must be absolute and resolved")
        if resolved_path in resolved_paths:
            raise InputError("preloaded workflow inputs contain a duplicate resolved path")
        resolved_paths.add(resolved_path)
        parts = posix_path.parts
        input_repository = resolved_path
        for _part in parts:
            input_repository = input_repository.parent
        if input_repository.joinpath(*parts) != resolved_path:
            raise InputError(
                "preloaded workflow input path does not match its repository-relative path"
            )
        if inferred_repository is None:
            inferred_repository = input_repository
        elif input_repository != inferred_repository:
            raise InputError("preloaded workflow inputs do not belong to one repository")
        if expected_repository is not None and input_repository != expected_repository:
            raise InputError("preloaded workflow inputs do not belong to the provided repository")

    if inferred_repository is None:  # pragma: no cover - guarded by the nonempty check
        raise InputError("preloaded workflow inputs must be a nonempty tuple")
    return expected_repository or inferred_repository


def _resolve_repository(repository: Path) -> Path:
    try:
        resolved = repository.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise InputError("workflow repository path cannot be resolved") from error
    try:
        is_directory = resolved.is_dir()
    except OSError as error:
        raise InputError("workflow repository path cannot be inspected") from error
    if not is_directory:
        raise InputError("workflow repository directory does not exist")
    return resolved


def _discover_workflows(repository: Path, selections: Sequence[Path]) -> list[Path]:
    if len(selections) > MAX_WORKFLOW_FILES:
        raise InputError(f"workflow selection exceeds the hard {MAX_WORKFLOW_FILES}-path limit")
    discovered: dict[str, Path] = {}
    for selection in selections:
        selected = _resolve_selection(repository, selection)
        try:
            is_file = selected.is_file()
            is_directory = selected.is_dir()
        except OSError as error:
            raise InputError("workflow input path cannot be inspected") from error
        if is_file:
            _add_workflow(repository, selected, discovered)
            continue
        if not is_directory:
            raise InputError(f"workflow input does not exist: {_display_selection(selection)}")

        matched = 0
        try:
            entries = selected.iterdir()
            for index, entry in enumerate(entries, start=1):
                if index > MAX_DIRECTORY_ENTRIES:
                    raise InputError(
                        f"workflow directory exceeds the hard {MAX_DIRECTORY_ENTRIES}-entry limit"
                    )
                if entry.suffix.lower() not in _WORKFLOW_SUFFIXES or not entry.is_file():
                    continue
                _add_workflow(repository, entry.resolve(), discovered)
                matched += 1
        except InputError:
            raise
        except (OSError, RuntimeError) as error:
            raise InputError("workflow directory cannot be read") from error
        if matched == 0:
            raise InputError(
                f"workflow directory contains no .yml or .yaml files: "
                f"{_display_selection(selection)}"
            )

    if not discovered:
        raise InputError("no workflow files were selected")
    paths = [discovered[key] for key in sorted(discovered)]
    if len(paths) > MAX_WORKFLOW_FILES:
        raise InputError(f"workflow selection exceeds the hard {MAX_WORKFLOW_FILES}-file limit")
    return paths


def _resolve_selection(repository: Path, selection: Path) -> Path:
    try:
        candidate = selection.expanduser()
        if not candidate.is_absolute():
            candidate = repository / candidate
        resolved = candidate.resolve()
        resolved.relative_to(repository)
    except (OSError, RuntimeError, ValueError) as error:
        raise InputError("workflow inputs must resolve inside the repository") from error
    return resolved


def _add_workflow(repository: Path, path: Path, discovered: dict[str, Path]) -> None:
    if path.suffix.lower() not in _WORKFLOW_SUFFIXES:
        raise InputError("workflow files must use a .yml or .yaml extension")
    try:
        relative = path.relative_to(repository).as_posix()
    except ValueError as error:
        raise InputError("workflow inputs must resolve inside the repository") from error
    validate_workflow_relative_path(relative)
    if relative not in discovered and len(discovered) >= MAX_WORKFLOW_FILES:
        raise InputError(f"workflow selection exceeds the hard {MAX_WORKFLOW_FILES}-file limit")
    discovered[relative] = path


def validate_workflow_relative_path(relative_path: str) -> None:
    """Require one bounded, UTF-8 repository-relative workflow path."""
    parts = PurePosixPath(relative_path).parts
    try:
        encoded_length = len(relative_path.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise InputError("workflow path is not valid UTF-8") from error
    if (
        not parts
        or PurePosixPath(relative_path).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or encoded_length > MAX_WORKFLOW_PATH_BYTES
        or len(parts) > MAX_WORKFLOW_PATH_COMPONENTS
    ):
        raise InputError("workflow path is not a bounded repository-relative path")


def _display_selection(selection: Path) -> str:
    return safe_error_text(selection, maximum=200)
