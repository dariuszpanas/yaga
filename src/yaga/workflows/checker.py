"""Discovery and aggregation for GitHub workflow reference policy."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yaga.errors import InputError, safe_error_text
from yaga.files import read_file_prefix
from yaga.workflows.models import WorkflowReport, WorkflowResult
from yaga.workflows.references import check_reference
from yaga.workflows.yaml import MAX_WORKFLOW_BYTES, parse_workflow

MAX_WORKFLOW_FILES = 128
MAX_DIRECTORY_ENTRIES = 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_TOTAL_NODES = 100_000
MAX_TOTAL_REFERENCES = 4096
MAX_TOTAL_DIAGNOSTICS = 512
_WORKFLOW_SUFFIXES = frozenset({".yaml", ".yml"})
_DEFAULT_DIRECTORY = Path(".github/workflows")


def check_workflows(
    repository: Path,
    selections: Sequence[Path] = (),
) -> WorkflowReport:
    """Check selected workflow files or the default GitHub workflow directory."""
    repo = _resolve_repository(repository)
    paths = _discover_workflows(repo, selections or (_DEFAULT_DIRECTORY,))
    results: list[WorkflowResult] = []
    total_bytes = 0
    total_nodes = 0
    total_references = 0
    total_diagnostics = 0

    for path in paths:
        relative = path.relative_to(repo).as_posix()
        try:
            raw = read_file_prefix(path, maximum=MAX_WORKFLOW_BYTES)
        except OSError as error:
            display_path = safe_error_text(relative, maximum=200)
            raise InputError(f"cannot read workflow {display_path}") from error
        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_BYTES:
            raise InputError(f"workflow inputs exceed the hard {MAX_TOTAL_BYTES}-byte total limit")

        parsed = parse_workflow(raw, label=relative)
        total_nodes += parsed.node_count
        if total_nodes > MAX_TOTAL_NODES:
            raise InputError(f"workflow inputs exceed the hard {MAX_TOTAL_NODES}-node total limit")
        total_references += len(parsed.references)
        if total_references > MAX_TOTAL_REFERENCES:
            raise InputError(
                f"workflow inputs exceed the hard {MAX_TOTAL_REFERENCES}-reference total limit"
            )

        diagnostics = list(parsed.diagnostics)
        diagnostics.extend(
            diagnostic
            for reference in parsed.references
            if (diagnostic := check_reference(reference)) is not None
        )
        diagnostics.sort(key=lambda item: (item.line, item.column, item.code, item.message))
        total_diagnostics += len(diagnostics)
        if total_diagnostics > MAX_TOTAL_DIAGNOSTICS:
            raise InputError(
                f"workflow inputs exceed the hard {MAX_TOTAL_DIAGNOSTICS}-diagnostic total limit"
            )
        results.append(
            WorkflowResult(
                path=relative,
                references_checked=len(parsed.references),
                diagnostics=tuple(diagnostics),
            )
        )

    return WorkflowReport(results=tuple(results))


def _resolve_repository(repository: Path) -> Path:
    try:
        resolved = repository.expanduser().resolve()
    except (OSError, RuntimeError) as error:
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
    if relative not in discovered and len(discovered) >= MAX_WORKFLOW_FILES:
        raise InputError(f"workflow selection exceeds the hard {MAX_WORKFLOW_FILES}-file limit")
    discovered[relative] = path


def _display_selection(selection: Path) -> str:
    return safe_error_text(selection, maximum=200)
