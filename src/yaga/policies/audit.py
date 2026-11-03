"""Audit YAGA TOML policy files for validity, required-version floors, and schema currency."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yaga.agent_review.policy import load_policy as load_agent_review_policy
from yaga.branches.policy import load_branch_policy
from yaga.changes.policy import load_change_policy
from yaga.commits.config import load_config
from yaga.descriptions.policy import load_description_policy
from yaga.errors import ConfigurationError, InputError, YagaError, safe_error_text
from yaga.files import read_file_prefix
from yaga.modes.policy import load_mode_policy
from yaga.paths.policy import load_path_policy
from yaga.policies.schemas import POLICY_SPECS, PolicyKind
from yaga.repository.plan import load_repository_check_plan
from yaga.sizes.policy import load_size_policy
from yaga.test_planning.policy import load_test_policy
from yaga.trees.policy import load_tree_policy

MAX_DISCOVERED_POLICIES = 128
MAX_AUDIT_FILE_BYTES = 1024 * 1024

_DISTINCTIVE_VERSION_KEYS: tuple[tuple[str, PolicyKind], ...] = (
    ("plan-version", PolicyKind.REPO),
    ("branch-policy-version", PolicyKind.BRANCH),
    ("change-policy-version", PolicyKind.CHANGE),
    ("description-policy-version", PolicyKind.DESCRIPTION),
    ("mode-policy-version", PolicyKind.MODE),
    ("path-policy-version", PolicyKind.PATH),
    ("size-policy-version", PolicyKind.SIZE),
    ("tree-policy-version", PolicyKind.TREE),
    ("test-policy-version", PolicyKind.TEST),
)

_FILENAME_HINTS: dict[str, PolicyKind] = {
    ".yaga.toml": PolicyKind.COMMIT,
    "branch-policy.toml": PolicyKind.BRANCH,
    "change-policy.toml": PolicyKind.CHANGE,
    "description-policy.toml": PolicyKind.DESCRIPTION,
    "mode-policy.toml": PolicyKind.MODE,
    "path-policy.toml": PolicyKind.PATH,
    "size-policy.toml": PolicyKind.SIZE,
    "tree-policy.toml": PolicyKind.TREE,
    "agent-review-policy.toml": PolicyKind.AGENT_REVIEW,
    "test-policy.toml": PolicyKind.TEST,
}


@dataclass(frozen=True, slots=True)
class PolicyAuditEntry:
    """Validation and schema-version currency result for one policy section."""

    path: Path
    kind: str
    version_key: str | None
    source_version: int | None
    current_version: int | None
    required_version: str | None
    status: str  # "current" | "deprecated" | "invalid"
    message: str


@dataclass(frozen=True, slots=True)
class PolicyAuditReport:
    """Complete policy audit report across explicit or discovered TOML files."""

    entries: tuple[PolicyAuditEntry, ...]
    strict: bool = False

    @property
    def current_count(self) -> int:
        return sum(1 for item in self.entries if item.status == "current")

    @property
    def deprecated_count(self) -> int:
        return sum(1 for item in self.entries if item.status == "deprecated")

    @property
    def invalid_count(self) -> int:
        return sum(1 for item in self.entries if item.status == "invalid")

    @property
    def valid(self) -> bool:
        if self.invalid_count > 0:
            return False
        if self.strict and self.deprecated_count > 0:
            return False
        return True

    @property
    def exit_code(self) -> int:
        if self.invalid_count > 0:
            return 2
        if self.strict and self.deprecated_count > 0:
            return 1
        return 0


def audit_policies(
    *,
    repository: Path = Path("."),
    paths: Sequence[Path] = (),
    strict: bool = False,
) -> PolicyAuditReport:
    """Audit explicit policy files or discover all YAGA policy files in repository."""
    if paths:
        if len(paths) > MAX_DISCOVERED_POLICIES:
            raise InputError(
                f"cannot audit more than {MAX_DISCOVERED_POLICIES} policy paths at once"
            )
        selected_paths = _normalize_explicit_paths(paths)
        resolved_repo = _try_resolve_dir(repository)
    else:
        resolved_repo = _require_repository_dir(repository)
        selected_paths = discover_repository_policy_files(resolved_repo)
        if not selected_paths:
            raise InputError(
                f"no YAGA policy files found in {safe_error_text(resolved_repo, maximum=300)}; "
                "run 'yaga policy init' or pass explicit file paths"
            )

    entries: list[PolicyAuditEntry] = []
    for path in selected_paths:
        entries.extend(_audit_file(path, repository=resolved_repo))
    return PolicyAuditReport(entries=tuple(entries), strict=strict)


def discover_repository_policy_files(repository: Path) -> tuple[Path, ...]:
    """Discover .yaga.toml, pyproject.toml [tool.yaga], and .yaga/**/*.toml inside repository."""
    discovered: list[Path] = []

    dot_yaga_toml = repository / ".yaga.toml"
    if dot_yaga_toml.exists() or dot_yaga_toml.is_symlink():
        _ensure_within_repository(dot_yaga_toml, repository)
        discovered.append(dot_yaga_toml.resolve())

    pyproject = repository / "pyproject.toml"
    if (pyproject.exists() or pyproject.is_symlink()) and _pyproject_has_yaga_table(
        pyproject, repository=repository
    ):
        discovered.append(pyproject.resolve())

    yaga_dir = repository / ".yaga"
    if yaga_dir.exists() or yaga_dir.is_symlink():
        _ensure_within_repository(yaga_dir, repository)
        resolved_yaga_dir = yaga_dir.resolve()
        if resolved_yaga_dir.is_dir():
            discovered.extend(_collect_toml_tree(resolved_yaga_dir, repository=repository))

    if len(discovered) > MAX_DISCOVERED_POLICIES:
        raise ConfigurationError(
            f"discovered policy count exceeds {MAX_DISCOVERED_POLICIES} files in "
            f"{safe_error_text(repository, maximum=300)}"
        )
    return tuple(discovered)


def _collect_toml_tree(directory: Path, *, repository: Path) -> list[Path]:
    collected: list[tuple[str, Path]] = []
    stack = [directory]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise ConfigurationError(
                f"cannot scan policy directory {safe_error_text(current, maximum=300)}"
            ) from error
        for child in children:
            _ensure_within_repository(child, repository)
            resolved_child = child.resolve()
            if resolved_child.is_dir():
                stack.append(resolved_child)
            elif child.suffix.lower() == ".toml":
                rel = resolved_child.relative_to(repository).as_posix()
                collected.append((rel, resolved_child))
                if len(collected) > MAX_DISCOVERED_POLICIES:
                    raise ConfigurationError(
                        f"discovered policy count exceeds {MAX_DISCOVERED_POLICIES} files in "
                        f"{safe_error_text(repository, maximum=300)}"
                    )
    collected.sort(key=lambda pair: pair[0])
    return [path for _rel, path in collected]


def _audit_file(path: Path, *, repository: Path | None) -> list[PolicyAuditEntry]:
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        return [
            PolicyAuditEntry(
                path=path,
                kind="unknown",
                version_key=None,
                source_version=None,
                current_version=None,
                required_version=None,
                status="invalid",
                message=f"cannot resolve policy path: {safe_error_text(error, maximum=300)}",
            )
        ]

    try:
        raw = read_file_prefix(resolved, maximum=MAX_AUDIT_FILE_BYTES)
    except OSError as error:
        return [
            PolicyAuditEntry(
                path=resolved,
                kind=_hint_kind_name(resolved),
                version_key=None,
                source_version=None,
                current_version=None,
                required_version=None,
                status="invalid",
                message=f"cannot read policy file: {safe_error_text(error, maximum=300)}",
            )
        ]
    if len(raw) > MAX_AUDIT_FILE_BYTES:
        return [
            PolicyAuditEntry(
                path=resolved,
                kind=_hint_kind_name(resolved),
                version_key=None,
                source_version=None,
                current_version=None,
                required_version=None,
                status="invalid",
                message=f"policy file exceeds {MAX_AUDIT_FILE_BYTES} bytes",
            )
        ]
    try:
        document = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as error:
        return [
            PolicyAuditEntry(
                path=resolved,
                kind=_hint_kind_name(resolved),
                version_key=None,
                source_version=None,
                current_version=None,
                required_version=None,
                status="invalid",
                message=f"invalid UTF-8 TOML: {safe_error_text(error, maximum=300)}",
            )
        ]

    kinds = _classify_document(resolved, document)
    if not kinds:
        return [
            PolicyAuditEntry(
                path=resolved,
                kind="unknown",
                version_key=None,
                source_version=None,
                current_version=None,
                required_version=None,
                status="invalid",
                message=(
                    "unrecognized YAGA policy file; expected a known *-version key "
                    "or [agent-review] table"
                ),
            )
        ]

    return [_audit_kind(kind, resolved, document, repository=repository) for kind in kinds]


def _classify_document(path: Path, document: Mapping[str, Any]) -> tuple[PolicyKind, ...]:
    if path.name == "pyproject.toml":
        tool = document.get("tool")
        yaga_table = tool.get("yaga") if isinstance(tool, dict) else None
        if isinstance(yaga_table, dict) and "agent-review" in yaga_table:
            if any(k in yaga_table for k in ("config-version", "commit", "required-version")):
                return (PolicyKind.COMMIT, PolicyKind.AGENT_REVIEW)
            return (PolicyKind.AGENT_REVIEW,)
        return (PolicyKind.COMMIT,)

    for version_key, kind in _DISTINCTIVE_VERSION_KEYS:
        if version_key in document:
            return (kind,)

    has_commit = "config-version" in document or "commit" in document
    has_agent_review = "agent-review" in document
    if has_commit and has_agent_review:
        return (PolicyKind.COMMIT, PolicyKind.AGENT_REVIEW)
    if has_commit:
        return (PolicyKind.COMMIT,)
    if has_agent_review:
        return (PolicyKind.AGENT_REVIEW,)

    hint = _infer_kind_from_path(path)
    if hint is not None:
        return (hint,)
    return ()


def _audit_kind(
    kind: PolicyKind,
    path: Path,
    document: Mapping[str, Any],
    *,
    repository: Path | None,
) -> PolicyAuditEntry:
    spec = POLICY_SPECS[kind]
    section = _extract_section(kind, path, document)
    raw_version = section.get(spec.version_key) if isinstance(section, Mapping) else None
    if raw_version is None and spec.family.missing_version is not None:
        source_version: int | None = int(spec.family.missing_version)
    elif type(raw_version) is int:
        source_version = raw_version
    else:
        source_version = None

    raw_req = _extract_required_version(kind, path, document, section)
    required_version = raw_req if isinstance(raw_req, str) else None

    try:
        actual_version = _run_domain_loader(kind, path, repository=repository)
    except YagaError as error:
        return PolicyAuditEntry(
            path=path,
            kind=kind.value,
            version_key=spec.version_key,
            source_version=source_version,
            current_version=spec.current_version_int,
            required_version=required_version,
            status="invalid",
            message=safe_error_text(error, maximum=400),
        )

    if actual_version < spec.current_version_int:
        return PolicyAuditEntry(
            path=path,
            kind=kind.value,
            version_key=spec.version_key,
            source_version=actual_version,
            current_version=spec.current_version_int,
            required_version=required_version,
            status="deprecated",
            message=(
                f"{spec.version_key} = {actual_version} is deprecated; "
                f"latest schema version is {spec.current_version_int}"
            ),
        )
    return PolicyAuditEntry(
        path=path,
        kind=kind.value,
        version_key=spec.version_key,
        source_version=actual_version,
        current_version=spec.current_version_int,
        required_version=required_version,
        status="current",
        message=f"{spec.version_key} = {actual_version} is current",
    )


def _run_domain_loader(kind: PolicyKind, path: Path, *, repository: Path | None) -> int:
    start = repository if repository is not None else path.parent
    if kind is PolicyKind.COMMIT:
        return load_config(path, start=start).policy.config_version
    if kind is PolicyKind.REPO:
        return load_repository_check_plan(path).plan_version
    if kind is PolicyKind.BRANCH:
        return load_branch_policy(path).policy.branch_policy_version
    if kind is PolicyKind.CHANGE:
        return load_change_policy(path).policy.change_policy_version
    if kind is PolicyKind.DESCRIPTION:
        return load_description_policy(path).policy.description_policy_version
    if kind is PolicyKind.MODE:
        return load_mode_policy(path).policy.mode_policy_version
    if kind is PolicyKind.PATH:
        return load_path_policy(path).policy.path_policy_version
    if kind is PolicyKind.SIZE:
        return load_size_policy(path).policy.size_policy_version
    if kind is PolicyKind.TREE:
        return load_tree_policy(path).policy.tree_policy_version
    if kind is PolicyKind.AGENT_REVIEW:
        return load_agent_review_policy(path).version
    if kind is PolicyKind.TEST:
        return load_test_policy(path).policy.test_policy_version
    raise AssertionError(f"unhandled PolicyKind: {kind!r}")


def _extract_section(
    kind: PolicyKind,
    path: Path,
    document: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    root: Any = document
    if path.name == "pyproject.toml":
        tool = document.get("tool")
        root = tool.get("yaga") if isinstance(tool, Mapping) else None
    if not isinstance(root, Mapping):
        return None
    if kind is PolicyKind.AGENT_REVIEW:
        sub = root.get("agent-review")
        return sub if isinstance(sub, Mapping) else None
    return root


def _extract_required_version(
    kind: PolicyKind,
    path: Path,
    document: Mapping[str, Any],
    section: Mapping[str, Any] | None,
) -> object:
    if isinstance(section, Mapping) and "required-version" in section:
        return section["required-version"]
    if kind is PolicyKind.AGENT_REVIEW:
        root: Any = document
        if path.name == "pyproject.toml":
            tool = document.get("tool")
            root = tool.get("yaga") if isinstance(tool, Mapping) else None
        if isinstance(root, Mapping) and "required-version" in root:
            return root["required-version"]
    return None


def _infer_kind_from_path(path: Path) -> PolicyKind | None:
    direct = _FILENAME_HINTS.get(path.name.lower())
    if direct is not None:
        return direct
    parts = [part.lower() for part in path.parts]
    if "checks" in parts and path.suffix.lower() == ".toml":
        return PolicyKind.REPO
    return None


def _hint_kind_name(path: Path) -> str:
    hint = _infer_kind_from_path(path)
    return hint.value if hint is not None else "unknown"


def _pyproject_has_yaga_table(pyproject: Path, *, repository: Path) -> bool:
    _ensure_within_repository(pyproject, repository)
    try:
        raw = read_file_prefix(pyproject.resolve(), maximum=MAX_AUDIT_FILE_BYTES)
    except OSError:
        return False
    if len(raw) > MAX_AUDIT_FILE_BYTES:
        return True
    try:
        doc = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError):
        return False
    tool = doc.get("tool")
    return isinstance(tool, dict) and "yaga" in tool


def _ensure_within_repository(candidate: Path, repository: Path) -> None:
    try:
        resolved = candidate.resolve()
        resolved.relative_to(repository)
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigurationError(
            f"policy path escapes repository {safe_error_text(repository, maximum=300)}: "
            f"{safe_error_text(candidate, maximum=300)}"
        ) from error


def _normalize_explicit_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[Path] = set()
    for item in paths:
        try:
            key = item.expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            key = item
        if key not in seen:
            seen.add(key)
            result.append(item)
    return tuple(result)


def _require_repository_dir(repository: Path) -> Path:
    try:
        resolved = repository.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise InputError(
            f"cannot resolve repository {safe_error_text(repository, maximum=300)}"
        ) from error
    if not resolved.is_dir():
        raise InputError(f"repository is not a directory: {safe_error_text(resolved, maximum=300)}")
    return resolved


def _try_resolve_dir(repository: Path) -> Path | None:
    try:
        resolved = repository.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_dir() else None
