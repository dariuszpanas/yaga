"""Starter TOML generation and atomic initialization for all YAGA policies."""

from __future__ import annotations

import os
import tempfile
import tomllib
from collections.abc import Collection, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from yaga.agent_review.policy import load_policy as load_agent_review_policy
from yaga.branches.policy import load_branch_policy
from yaga.changes.policy import load_change_policy
from yaga.commits.config import MAX_CONFIG_BYTES, load_config
from yaga.descriptions.policy import load_description_policy
from yaga.errors import ConfigurationError, InputError, safe_error_text
from yaga.files import read_file_prefix
from yaga.modes.policy import load_mode_policy
from yaga.paths.policy import load_path_policy
from yaga.policies.schemas import POLICY_SPECS, PolicyKind
from yaga.pull_request_triage.policy import load_triage_policy
from yaga.repository.plan import load_repository_check_plan
from yaga.sizes.policy import load_size_policy
from yaga.test_planning.policy import load_test_policy
from yaga.trees.policy import load_tree_policy

_COMMITTED_TREE_POLICIES = (
    PolicyKind.MODE,
    PolicyKind.PATH,
    PolicyKind.SIZE,
    PolicyKind.TREE,
)


@dataclass(frozen=True, slots=True)
class InitializedPolicyItem:
    """Result of initializing (or previewing/skipping) one YAGA policy file."""

    kind: PolicyKind
    path: Path
    schema_version: int
    status: str  # "created" | "dry-run" | "skipped"


@dataclass(frozen=True, slots=True)
class PolicyInitReport:
    """Summary of a yaga policy init invocation."""

    repository: Path
    dry_run: bool
    items: tuple[InitializedPolicyItem, ...]


def existing_policy_status(
    repository: Path,
    *,
    kinds: Collection[PolicyKind] | None = None,
) -> dict[PolicyKind, Path | None]:
    """Inspect which policy targets already exist in repository without mutating state."""
    resolved_repo = _resolve_repository_directory(repository)
    target_kinds = tuple(POLICY_SPECS.keys()) if kinds is None else tuple(kinds)
    result: dict[PolicyKind, Path | None] = {}
    for kind in target_kinds:
        spec = POLICY_SPECS[kind]
        if kind is PolicyKind.COMMIT:
            result[kind] = _existing_commit_policy_path(resolved_repo)
            continue
        target = resolved_repo / spec.default_relpath
        result[kind] = target if _target_exists(target) else None
    return result


def _existing_commit_policy_path(resolved_repo: Path) -> Path | None:
    dot_yaga = resolved_repo / POLICY_SPECS[PolicyKind.COMMIT].default_relpath
    if _target_exists(dot_yaga):
        return dot_yaga

    pyproject = resolved_repo / "pyproject.toml"
    if not _target_exists(pyproject):
        return None

    display_pyproject = safe_error_text(pyproject, maximum=300)
    try:
        resolved_pyproject = pyproject.resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigurationError(f"cannot resolve configuration {display_pyproject}") from error
    if not _is_within(resolved_pyproject, resolved_repo):
        raise ConfigurationError(
            f"configuration escapes repository {safe_error_text(resolved_repo, maximum=300)}: "
            f"{display_pyproject}"
        )
    if not resolved_pyproject.is_file():
        raise ConfigurationError(f"cannot read configuration {display_pyproject}")

    try:
        raw = read_file_prefix(resolved_pyproject, maximum=MAX_CONFIG_BYTES)
    except OSError as error:
        raise ConfigurationError(
            f"cannot read configuration {display_pyproject}: {safe_error_text(error, maximum=300)}"
        ) from error
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigurationError(
            f"configuration exceeds {MAX_CONFIG_BYTES} bytes: {display_pyproject}"
        )
    try:
        document = tomllib.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as error:
        raise ConfigurationError(
            f"configuration is not valid UTF-8: {display_pyproject}"
        ) from error
    except (tomllib.TOMLDecodeError, RecursionError) as error:
        raise ConfigurationError(
            f"invalid TOML in {display_pyproject}: {safe_error_text(error, maximum=300)}"
        ) from error

    tool = document.get("tool")
    if tool is None:
        return None
    if not isinstance(tool, dict):
        raise ConfigurationError(f"[tool] must be a table in {display_pyproject}")
    if "yaga" in tool:
        return pyproject
    return None


def _target_exists(path: Path) -> bool:
    try:
        return path.is_symlink() or path.exists()
    except OSError as error:
        raise ConfigurationError(
            f"cannot inspect policy target {safe_error_text(path, maximum=300)}"
        ) from error


def render_starter_toml(
    kind: PolicyKind,
    *,
    active_tree_policies: frozenset[PolicyKind] = frozenset(),
) -> str:
    """Render an editable starter TOML document stamped with the latest schema version."""
    spec = POLICY_SPECS[kind]
    version = spec.current_version_int

    if kind is PolicyKind.COMMIT:
        return (
            f"# YAGA commit and pull-request title policy\n"
            f"config-version = {version}\n\n"
            "[commit]\n"
            "allowed-types = [\n"
            '    "build",\n'
            '    "chore",\n'
            '    "ci",\n'
            '    "docs",\n'
            '    "feat",\n'
            '    "fix",\n'
            '    "perf",\n'
            '    "refactor",\n'
            '    "revert",\n'
            '    "style",\n'
            '    "test",\n'
            "]\n"
            'type-case = "lower"\n'
            'scope-policy = "optional"\n'
            'scope-case = "lower"\n'
            "header-max-length = 100\n"
            "description-min-length = 3\n"
            'description-ending = "forbid"\n'
            'body-policy = "optional"\n'
            'body-paragraph-splitting = "check"\n'
            'dependabot-pull-requests = "check"\n'
            'merge-commits = "reject"\n'
            "ignored-headers = []\n"
            "max-commits = 64\n"
        )

    if kind is PolicyKind.REPO:
        providers = ["commit", "workflow", "workflow-security", "workflow-lint"]
        policy_lines: list[str] = []
        commented_lines: list[str] = []
        for tree_kind in _COMMITTED_TREE_POLICIES:
            tree_spec = POLICY_SPECS[tree_kind]
            line = f'{tree_kind.value}-policy = "{tree_spec.default_relpath}"'
            if tree_kind in active_tree_policies:
                providers.append(tree_kind.value)
                policy_lines.append(line)
            else:
                commented_lines.append(f"# {line}")
        checks_lines = ",\n".join(f'    "{provider}"' for provider in providers)
        lines = [
            "# YAGA repository check plan",
            f"plan-version = {version}",
            "checks = [",
            checks_lines,
            "]",
            'workflow-security-profile = "recommended-v3"',
        ]
        if policy_lines:
            lines.append("")
            lines.extend(policy_lines)
        if commented_lines:
            lines.append("")
            lines.append("# Uncomment after initializing committed-tree policies:")
            lines.extend(commented_lines)
        return "\n".join(lines) + "\n"

    if kind is PolicyKind.BRANCH:
        return (
            "# YAGA branch-name policy\n"
            f"branch-policy-version = {version}\n"
            "allowed-patterns = [\n"
            '    "main",\n'
            '    "feat/*",\n'
            '    "fix/*",\n'
            '    "docs/*",\n'
            '    "chore/*",\n'
            '    "refactor/*",\n'
            '    "test/*",\n'
            '    "ci/*",\n'
            '    "build/*",\n'
            '    "perf/*",\n'
            '    "dependabot/**",\n'
            "]\n"
        )

    if kind is PolicyKind.CHANGE:
        return (
            "# YAGA changed-path coupling policy\n"
            f"change-policy-version = {version}\n\n"
            "[[rules]]\n"
            'name = "source-requires-tests"\n'
            'when-any = ["src/**"]\n'
            'require-any = ["tests/**", "docs/**"]\n'
        )

    if kind is PolicyKind.DESCRIPTION:
        return (
            "# YAGA pull-request description policy\n"
            f"description-policy-version = {version}\n"
            "min-words = 8\n"
            "max-length = 65536\n"
            'placeholders = ["TODO", "TBD"]\n'
            "require-checked-tasks = false\n"
            'typos = "skip"\n'
            "heading-min-level = 2\n"
            "heading-max-length = 120\n"
        )

    if kind is PolicyKind.MODE:
        return (
            "# YAGA committed Git file-mode policy\n"
            f"mode-policy-version = {version}\n"
            'default-allowed-modes = ["regular"]\n\n'
            "[[path-overrides]]\n"
            'pattern = "**/*.sh"\n'
            'allowed-modes = ["regular", "executable"]\n'
        )

    if kind is PolicyKind.PATH:
        return (
            "# YAGA cross-platform committed-path policy\n"
            f"path-policy-version = {version}\n"
            'profile = "windows-compatible-v1"\n'
        )

    if kind is PolicyKind.SIZE:
        return (
            "# YAGA committed blob-size policy\n"
            f"size-policy-version = {version}\n"
            "default-max-blob-bytes = 1048576\n"
            "# max-total-blob-bytes = 67108864\n"
        )

    if kind is PolicyKind.TREE:
        return (
            "# YAGA tracked-tree policy\n"
            f"tree-policy-version = {version}\n"
            "required-paths = []\n"
            "forbidden-patterns = [\n"
            '    "**/.DS_Store",\n'
            '    "**/.env",\n'
            '    "**/.env.*",\n'
            '    "**/*.pem",\n'
            '    "**/*.key",\n'
            "]\n"
        )

    if kind is PolicyKind.AGENT_REVIEW:
        return (
            "# YAGA pull-request agent review policy\n"
            "[agent-review]\n"
            f"version = {version}\n"
            'required = ["codex"]\n'
            'aggregation = "all-required"\n\n'
            "[agent-review.agents.codex]\n"
            'preset = "codex"\n'
            'outcome = "review"\n'
            'publication = "review"\n'
        )

    if kind is PolicyKind.TRIAGE:
        return (
            "# Replace the repository ID with your verified GitHub repository ID.\n"
            "# No maintainer or automation is trusted until explicitly configured.\n"
            f"triage-policy-version = {version}\n"
            'host = "github.com"\n'
            "repository-id = 1\n"
            "maintainer-user-ids = []\n"
            "duplicate-candidates = 5\n"
        )

    if kind is PolicyKind.TEST:
        return (
            "# YAGA deterministic test qualification policy\n"
            f"test-policy-version = {version}\n"
            'unknown-paths = "full"\n\n'
            "[[suites]]\n"
            'id = "unit"\n'
            'binding = "pytest"\n\n'
            "[profiles.ci]\n"
            'mode = "selective"\n\n'
            "[profiles.full]\n"
            'mode = "full"\n\n'
            "[[rules]]\n"
            'id = "source-or-tests"\n'
            'when-any = ["src/**", "tests/**"]\n'
            'select = ["unit"]\n'
        )

    raise AssertionError(f"unhandled PolicyKind: {kind!r}")


def initialize_policies(
    repository: Path,
    kinds: Sequence[PolicyKind],
    *,
    dry_run: bool = False,
    skip_existing: bool = False,
) -> PolicyInitReport:
    """Validate and atomically publish one or more starter YAGA policy files."""
    if not kinds:
        raise InputError("select at least one policy to initialize")
    seen: list[PolicyKind] = []
    for kind in kinds:
        if kind in seen:
            raise InputError(f"duplicate policy selection: {kind.value}")
        seen.append(kind)

    resolved_repo = _resolve_repository_directory(repository)
    kinds_to_inspect = frozenset(seen) | (
        frozenset(_COMMITTED_TREE_POLICIES) if PolicyKind.REPO in seen else frozenset()
    )
    existing = existing_policy_status(resolved_repo, kinds=kinds_to_inspect)

    to_create: list[PolicyKind] = []
    items_by_kind: dict[PolicyKind, InitializedPolicyItem] = {}

    for kind in seen:
        spec = POLICY_SPECS[kind]
        existing_path = existing[kind]
        if existing_path is not None:
            if skip_existing:
                items_by_kind[kind] = InitializedPolicyItem(
                    kind=kind,
                    path=existing_path,
                    schema_version=spec.current_version_int,
                    status="skipped",
                )
                continue
            if kind is PolicyKind.COMMIT and existing_path.name == "pyproject.toml":
                raise ConfigurationError(
                    f"commit configuration already applies in {resolved_repo}: {existing_path}"
                )
            raise ConfigurationError(f"policy target already exists: {existing_path}")
        to_create.append(kind)

    active_tree_policies = frozenset(
        tree_kind
        for tree_kind in _COMMITTED_TREE_POLICIES
        if tree_kind in to_create or existing.get(tree_kind) is not None
    )

    staged: list[tuple[PolicyKind, Path, Path, int]] = []
    published: list[Path] = []
    with ExitStack() as stack:
        dry_run_dir = (
            Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="yaga-policy-init-")))
            if dry_run
            else None
        )
        try:
            for kind in to_create:
                spec = POLICY_SPECS[kind]
                target = _safe_repo_target(
                    resolved_repo, spec.default_relpath, create_parents=not dry_run
                )
                rendered = render_starter_toml(kind, active_tree_policies=active_tree_policies)
                content_bytes = rendered.encode("utf-8")
                staging_dir = (
                    dry_run_dir
                    if dry_run_dir is not None
                    else (target.parent if target.parent.is_dir() else resolved_repo)
                )
                temporary = _write_temp_file(
                    staging_dir, prefix=f".{target.name}.", content=content_bytes
                )
                staged.append((kind, temporary, target, spec.current_version_int))
                _validate_generated_policy(kind, temporary, repository=resolved_repo)

            if dry_run:
                for kind, _temporary, target, schema_version in staged:
                    items_by_kind[kind] = InitializedPolicyItem(
                        kind=kind,
                        path=target,
                        schema_version=schema_version,
                        status="dry-run",
                    )
            else:
                try:
                    for kind, temporary, target, schema_version in staged:
                        _publish_without_replacement(temporary, target)
                        published.append(target)
                        _validate_generated_policy(kind, target, repository=resolved_repo)
                        items_by_kind[kind] = InitializedPolicyItem(
                            kind=kind,
                            path=target,
                            schema_version=schema_version,
                            status="created",
                        )
                except BaseException:
                    for created_path in published:
                        _remove_temp_file(created_path)
                    raise
        finally:
            for _kind, temporary, _target, _ver in staged:
                _remove_temp_file(temporary)

    ordered_items = tuple(items_by_kind[kind] for kind in seen)
    return PolicyInitReport(repository=resolved_repo, dry_run=dry_run, items=ordered_items)


def _validate_generated_policy(kind: PolicyKind, path: Path, *, repository: Path) -> None:
    if kind is PolicyKind.COMMIT:
        load_config(path, start=repository)
    elif kind is PolicyKind.REPO:
        load_repository_check_plan(path)
    elif kind is PolicyKind.BRANCH:
        load_branch_policy(path)
    elif kind is PolicyKind.CHANGE:
        load_change_policy(path)
    elif kind is PolicyKind.DESCRIPTION:
        load_description_policy(path)
    elif kind is PolicyKind.MODE:
        load_mode_policy(path)
    elif kind is PolicyKind.PATH:
        load_path_policy(path)
    elif kind is PolicyKind.SIZE:
        load_size_policy(path)
    elif kind is PolicyKind.TREE:
        load_tree_policy(path)
    elif kind is PolicyKind.AGENT_REVIEW:
        load_agent_review_policy(path)
    elif kind is PolicyKind.TEST:
        load_test_policy(path)
    elif kind is PolicyKind.TRIAGE:
        load_triage_policy(path)
    else:
        raise AssertionError(f"unhandled PolicyKind: {kind!r}")


def _resolve_repository_directory(repository: Path) -> Path:
    try:
        resolved = repository.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigurationError(
            f"cannot resolve repository directory {safe_error_text(repository, maximum=300)}"
        ) from error
    if not resolved.is_dir():
        raise ConfigurationError(
            f"repository directory does not exist or is not a directory: "
            f"{safe_error_text(resolved, maximum=300)}"
        )
    return resolved


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_repo_target(repository: Path, relpath: str, *, create_parents: bool) -> Path:
    current = repository
    parts = Path(relpath).parts
    for directory_part in parts[:-1]:
        current = current / directory_part
        if current.is_symlink():
            raise ConfigurationError(
                f"refusing to write through symlinked directory: {safe_error_text(current, maximum=300)}"
            )
        if current.exists():
            resolved_dir = current.resolve()
            if not _is_within(resolved_dir, repository) or not resolved_dir.is_dir():
                raise ConfigurationError(
                    f"policy directory escapes repository or is not a directory: "
                    f"{safe_error_text(current, maximum=300)}"
                )
        elif create_parents:
            try:
                current.mkdir(parents=False, exist_ok=True)
            except OSError as error:
                raise ConfigurationError(
                    f"cannot create policy directory {safe_error_text(current, maximum=300)}"
                ) from error
    target = current / parts[-1]
    if target.exists() or target.is_symlink():
        raise ConfigurationError(f"policy target already exists: {target}")
    return target


def _write_temp_file(directory: Path, *, prefix: str, content: bytes) -> Path:
    try:
        descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    except OSError as error:
        raise ConfigurationError(
            f"cannot stage temporary policy file in {safe_error_text(directory, maximum=300)}"
        ) from error
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            written = stream.write(content)
            if written != len(content):
                raise OSError("policy write was incomplete")
            stream.flush()
            os.chmod(temporary, 0o644)
            os.fsync(stream.fileno())
    except BaseException as error:
        _remove_temp_file(temporary)
        if isinstance(error, OSError):
            raise ConfigurationError(
                f"cannot stage temporary policy file {safe_error_text(temporary, maximum=300)}"
            ) from error
        raise
    return temporary


def _publish_without_replacement(temporary: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise ConfigurationError(f"policy target already exists: {target}")
    try:
        if os.name == "nt":
            os.rename(temporary, target)
        else:
            os.link(temporary, target)
    except OSError as error:
        if target.exists() or target.is_symlink():
            raise ConfigurationError(f"policy target already exists: {target}") from error
        raise ConfigurationError(
            f"cannot publish policy file {safe_error_text(target, maximum=300)}"
        ) from error


def _remove_temp_file(temporary: Path) -> None:
    try:
        temporary.unlink()
    except OSError:
        pass
