"""Bounded repository snapshots for the pinned actionlint adapter."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType

from yaga.errors import InputError
from yaga.files import read_file_prefix
from yaga.workflows.inputs import (
    MAX_WORKFLOW_BYTES,
    MAX_WORKFLOW_PATH_BYTES,
    MAX_WORKFLOW_PATH_COMPONENTS,
    WorkflowInput,
    validate_workflow_relative_path,
)
from yaga.workflows.models import (
    ActionManifestDependency,
    ReferenceContext,
    WorkflowReference,
)
from yaga.workflows.yaml import WorkflowDocumentError, parse_action_manifest, parse_workflow

MAX_ACTIONLINT_SNAPSHOT_FILES = 256
MAX_ACTIONLINT_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_ACTIONLINT_SNAPSHOT_NODES = 100_000
MAX_ACTIONLINT_SNAPSHOT_REFERENCES = 4096
MAX_ACTIONLINT_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_ACTIONLINT_PATH_BYTES = MAX_WORKFLOW_PATH_BYTES
MAX_ACTIONLINT_PATH_COMPONENTS = MAX_WORKFLOW_PATH_COMPONENTS
MAX_ACTIONLINT_ARCHIVE_ENTRIES = 20_000
ACTIONLINT_CONFIG_PATHS = (".github/actionlint.yaml", ".github/actionlint.yml")
_ACTIONLINT_REMOTE_IMAGE_PREFIXES = (
    "docker://",
    "gcr.io/",
    "pkg.dev/",
    "ghcr.io/",
    "docker.io/",
)


@dataclass(frozen=True, slots=True)
class ActionlintSnapshot(Mapping[str, bytes]):
    """Immutable snapshot files plus explicit runtime-directory presence."""

    files: Mapping[str, bytes]
    directories: tuple[str, ...] = ()

    def __getitem__(self, key: str) -> bytes:
        return self.files[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.files)

    def __len__(self) -> int:
        return len(self.files)


def build_actionlint_snapshot(
    repository: Path,
    workflows: tuple[WorkflowInput, ...],
) -> ActionlintSnapshot:
    """Build a bounded repository-shaped snapshot for native actionlint resolution."""
    files: dict[str, bytes] = {}
    for workflow in workflows:
        validate_snapshot_relative_path(workflow.relative_path)
        files[workflow.relative_path] = workflow.content
    if len(files) > MAX_ACTIONLINT_SNAPSHOT_FILES:
        raise InputError("actionlint workspace exceeds its file limit")
    if sum(len(content) for content in files.values()) > MAX_ACTIONLINT_SNAPSHOT_BYTES:
        raise InputError("actionlint workspace exceeds its byte limit")

    for config_path in ACTIONLINT_CONFIG_PATHS:
        _add_snapshot_file(repository, config_path, files)
    configured = [path for path in ACTIONLINT_CONFIG_PATHS if path in files]
    if len(configured) > 1:
        raise InputError("repository contains both supported native actionlint config files")

    queue = [workflow.relative_path for workflow in workflows]
    processed: set[str] = set()
    total_nodes = 0
    total_references = 0
    action_manifests: set[str] = set()
    runtime_directories: set[str] = set()
    while queue:
        relative_path = queue.pop(0)
        if relative_path in processed:
            continue
        processed.add(relative_path)
        content = files[relative_path]
        try:
            parsed = parse_workflow(content, label=relative_path)
        except WorkflowDocumentError:
            # Preserve malformed-workflow handling as an actionlint finding rather than
            # converting its exit 1 contract into an input error during dependency discovery.
            continue
        total_nodes += parsed.node_count
        total_references += len(parsed.references)
        if total_nodes > MAX_ACTIONLINT_SNAPSHOT_NODES:
            raise InputError("actionlint workspace exceeds its YAML-node limit")
        if total_references > MAX_ACTIONLINT_SNAPSHOT_REFERENCES:
            raise InputError("actionlint workspace exceeds its reference limit")
        files[relative_path] = normalize_self_references(content, parsed.references)

        for reference in parsed.references:
            dependency = _resolve_local_reference_path(reference)
            if dependency is None:
                continue
            if reference.context is ReferenceContext.JOB:
                if _add_snapshot_file(repository, dependency, files):
                    queue.append(dependency)
                continue
            manifest_path = _add_local_action_manifest(
                repository,
                dependency,
                files,
            )
            if manifest_path is not None:
                action_manifests.add(manifest_path)

    for manifest_path in sorted(action_manifests):
        try:
            files[manifest_path].decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            # actionlint owns malformed action metadata; dependency discovery cannot
            # safely select runtime paths from a non-UTF-8 manifest.
            continue
        parsed_manifest = parse_action_manifest(files[manifest_path], label=manifest_path)
        total_nodes += parsed_manifest.node_count
        total_references += len(parsed_manifest.dependencies)
        if total_nodes > MAX_ACTIONLINT_SNAPSHOT_NODES:
            raise InputError("actionlint workspace exceeds its YAML-node limit")
        if total_references > MAX_ACTIONLINT_SNAPSHOT_REFERENCES:
            raise InputError("actionlint workspace exceeds its reference limit")
        for dependency in parsed_manifest.dependencies:
            runtime_path = action_runtime_path(manifest_path, dependency)
            if runtime_path is not None:
                _add_snapshot_presence(
                    repository,
                    runtime_path,
                    files,
                    runtime_directories,
                )

    return ActionlintSnapshot(
        files=MappingProxyType(dict(sorted(files.items()))),
        directories=tuple(sorted(runtime_directories)),
    )


def _add_local_action_manifest(
    repository: Path,
    target: str,
    files: dict[str, bytes],
) -> str | None:
    prefix = f"{target}/" if target else ""
    for filename in ("action.yaml", "action.yml"):
        manifest_path = f"{prefix}{filename}"
        _add_snapshot_file(repository, manifest_path, files)
        if manifest_path in files:
            return manifest_path
    return None


def action_runtime_path(
    manifest_path: str,
    dependency: ActionManifestDependency,
) -> str | None:
    value = dependency.value
    if not value:
        return None
    if dependency.field == "image" and value.startswith(_ACTIONLINT_REMOTE_IMAGE_PREFIXES):
        return None
    if "\\" in value or PurePosixPath(value).is_absolute():
        return None

    parts = list(PurePosixPath(manifest_path).parent.parts)
    for part in PurePosixPath(value).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        return None
    relative_path = PurePosixPath(*parts).as_posix()
    validate_snapshot_relative_path(relative_path)
    return relative_path


def validate_snapshot_relative_path(relative_path: str) -> None:
    validate_workflow_relative_path(relative_path)


def _resolve_local_reference_path(reference: WorkflowReference) -> str | None:
    """Resolve one native-local lint dependency without applying workflow policy."""
    value = reference.value
    if not value.startswith(("./", "$/")):
        return None
    expression_start = value.find("${{")
    expression_end = value.find("}}")
    if 0 <= expression_start < expression_end or "\x00" in value or "\\" in value:
        return None

    target = value[2:]
    if reference.context is ReferenceContext.JOB and (not target or "@" in target):
        return None
    if PureWindowsPath(target).drive:
        return None
    try:
        encoded_length = len(target.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise InputError("actionlint local reference path is not valid UTF-8") from error
    component_count = target.count("/") + (1 if target else 0)
    if (
        encoded_length > MAX_ACTIONLINT_PATH_BYTES
        or component_count > MAX_ACTIONLINT_PATH_COMPONENTS
    ):
        raise InputError("actionlint local reference path exceeds its bounded support limit")

    parts: list[str] = []
    for part in target.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)

    if not parts:
        return "" if reference.context is ReferenceContext.STEP else None
    relative_path = PurePosixPath(*parts).as_posix()
    validate_snapshot_relative_path(relative_path)
    return relative_path


def _add_snapshot_file(
    repository: Path,
    relative_path: str,
    files: dict[str, bytes],
) -> bool:
    if relative_path in files:
        return False
    validate_snapshot_relative_path(relative_path)
    parts = PurePosixPath(relative_path).parts
    candidate = repository.joinpath(*parts)
    try:
        exists = candidate.exists() or candidate.is_symlink()
        if not exists:
            return False
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repository)
        if not resolved.is_file():
            raise InputError("actionlint support path is not a file")
        content = read_file_prefix(resolved, maximum=MAX_WORKFLOW_BYTES)
    except InputError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise InputError("actionlint support file cannot be read safely") from error
    if len(content) > MAX_WORKFLOW_BYTES:
        raise InputError("actionlint support file exceeds its byte limit")
    if len(files) >= MAX_ACTIONLINT_SNAPSHOT_FILES:
        raise InputError("actionlint workspace exceeds its file limit")
    if sum(len(item) for item in files.values()) + len(content) > MAX_ACTIONLINT_SNAPSHOT_BYTES:
        raise InputError("actionlint workspace exceeds its byte limit")
    files[relative_path] = content
    return True


def _add_snapshot_presence(
    repository: Path,
    relative_path: str,
    files: dict[str, bytes],
    directories: set[str],
) -> bool:
    """Stage only existence for actionlint runtime-file checks, never runtime contents."""
    if relative_path in files:
        return False
    validate_snapshot_relative_path(relative_path)
    candidate = repository.joinpath(*PurePosixPath(relative_path).parts)
    try:
        if not (candidate.exists() or candidate.is_symlink()):
            return False
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repository)
    except (OSError, RuntimeError, ValueError):
        return False
    if resolved.is_dir():
        if relative_path in directories:
            return False
        if len(files) + len(directories) >= MAX_ACTIONLINT_SNAPSHOT_FILES:
            raise InputError("actionlint workspace exceeds its file limit")
        directories.add(relative_path)
        return True
    elif not resolved.is_file():
        return False
    if len(files) + len(directories) >= MAX_ACTIONLINT_SNAPSHOT_FILES:
        raise InputError("actionlint workspace exceeds its file limit")
    files[relative_path] = b""
    return True


def normalize_self_references(
    content: bytes,
    references: tuple[WorkflowReference, ...],
) -> bytes:
    """Translate supported ``$/`` references for pinned actionlint without moving marks."""
    has_bom = content.startswith(b"\xef\xbb\xbf")
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return content
    spans: dict[tuple[int, int], str] = {}
    for reference in references:
        if not reference.value.startswith("$/") or _resolve_local_reference_path(reference) is None:
            continue
        if reference.source_start is None or reference.source_end is None:
            raise InputError("actionlint self-reference source location is unavailable")
        span = (reference.source_start, reference.source_end)
        previous = spans.setdefault(span, reference.value)
        if previous != reference.value:
            raise InputError("actionlint self-reference source location is ambiguous")

    changed = False
    for (start, end), value in sorted(spans.items(), reverse=True):
        if not 0 <= start < end <= len(text):
            raise InputError("actionlint self-reference source location is invalid")
        source = text[start:end]
        if source.count(value) != 1:
            raise InputError("actionlint self-reference cannot be normalized safely")
        value_index = start + source.index(value)
        text = f"{text[:value_index]}.{text[value_index + 1 :]}"
        changed = True

    if not changed:
        return content
    normalized = text.encode("utf-8")
    return b"\xef\xbb\xbf" + normalized if has_bom else normalized


def build_actionlint_archive(files: Mapping[str, bytes]) -> bytes:
    """Return a deterministic regular-file-only tar stream for ``docker cp -``."""
    file_paths = frozenset(files)
    fixed_directories = {".git", ".github", ".github/workflows"}
    explicit_directories = (
        set(files.directories) if isinstance(files, ActionlintSnapshot) else set()
    )
    for directory in explicit_directories:
        validate_snapshot_relative_path(directory)
    if fixed_directories & file_paths:
        raise InputError("actionlint workspace archive contains a file-directory collision")
    if explicit_directories & file_paths:
        raise InputError("actionlint workspace archive contains a file-directory collision")
    for relative_path in file_paths:
        validate_snapshot_relative_path(relative_path)
        parents = PurePosixPath(relative_path).parents
        if any(parent.as_posix() in file_paths for parent in parents if parent.as_posix() != "."):
            raise InputError("actionlint workspace archive contains a file-directory collision")

    for directory in explicit_directories:
        parents = PurePosixPath(directory).parents
        if any(parent.as_posix() in file_paths for parent in parents if parent.as_posix() != "."):
            raise InputError("actionlint workspace archive contains a file-directory collision")

    directories = fixed_directories | explicit_directories
    for relative_path in files:
        parts = PurePosixPath(relative_path).parts[:-1]
        for index in range(1, len(parts) + 1):
            directories.add(PurePosixPath(*parts[:index]).as_posix())
            if len(directories) + len(files) > MAX_ACTIONLINT_ARCHIVE_ENTRIES:
                raise InputError("actionlint workspace archive exceeds its entry limit")
    if len(directories) + len(files) > MAX_ACTIONLINT_ARCHIVE_ENTRIES:
        raise InputError("actionlint workspace archive exceeds its entry limit")
    stream = io.BytesIO()
    try:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for directory in sorted(directories):
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                info.mode = 0o555
                info.uid = 0
                info.gid = 0
                info.mtime = 0
                archive.addfile(info)
            for relative_path in sorted(files):
                validate_snapshot_relative_path(relative_path)
                content = files[relative_path]
                info = tarfile.TarInfo(relative_path)
                info.size = len(content)
                info.mode = 0o444
                info.uid = 0
                info.gid = 0
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))
    except (OSError, ValueError, tarfile.TarError) as error:
        raise InputError("actionlint workspace archive cannot be created") from error
    result = stream.getvalue()
    if len(result) > MAX_ACTIONLINT_ARCHIVE_BYTES:
        raise InputError("actionlint workspace archive exceeds its byte limit")
    return result
