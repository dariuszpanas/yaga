"""Strict parsing for explicit, portable repository-check plans."""

from __future__ import annotations

import tomllib
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

from yaga.errors import ConfigurationError, InputError, safe_error_text
from yaga.files import read_file_prefix
from yaga.repository.models import RepositoryCheckPlan, RepositoryProvider
from yaga.workflows.inputs import (
    MAX_WORKFLOW_FILES,
    MAX_WORKFLOW_PATH_BYTES,
    validate_workflow_relative_path,
)
from yaga.workflows.security_models import (
    WorkflowSecurityProfile,
    WorkflowSecurityRule,
)
from yaga.workflows.security_rules import MAX_SECURITY_RULES, normalize_security_rules

MAX_REPOSITORY_PLAN_BYTES = 64 * 1024

_PLAN_KEYS = frozenset(
    {
        "plan-version",
        "checks",
        "workflow-paths",
        "workflow-security-profile",
        "workflow-security-rules",
    }
)
_WINDOWS_FORBIDDEN_PATH_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_PATH_NAMES = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
_WORKFLOW_PROVIDERS = frozenset(
    {
        RepositoryProvider.WORKFLOW,
        RepositoryProvider.WORKFLOW_SECURITY,
        RepositoryProvider.WORKFLOW_LINT,
    }
)


def load_repository_check_plan(path: Path) -> RepositoryCheckPlan:
    """Read and strictly normalize one explicitly selected TOML plan."""
    resolved = _resolve_plan_path(path)
    document = _read_plan_document(resolved)
    unknown = sorted(set(document) - _PLAN_KEYS)
    if unknown:
        names = ", ".join(unknown)
        raise ConfigurationError(f"unknown repository check plan key(s) in {resolved}: {names}")

    plan_version = _plan_version(document, path=resolved)
    checks = _checks(document, path=resolved)
    workflow_paths = (
        _workflow_paths(document["workflow-paths"], path=resolved)
        if "workflow-paths" in document
        else ()
    )
    profile, rules = _security_selection(document, path=resolved)

    selected = frozenset(checks)
    if workflow_paths and selected.isdisjoint(_WORKFLOW_PROVIDERS):
        raise ConfigurationError(f"workflow-paths require a workflow check provider in {resolved}")
    if (profile is not None or rules) and RepositoryProvider.WORKFLOW_SECURITY not in selected:
        raise ConfigurationError(
            f"workflow security profile and rules require the workflow-security check in {resolved}"
        )

    return RepositoryCheckPlan(
        plan_version=plan_version,
        checks=checks,
        workflow_paths=workflow_paths,
        workflow_security_profile=profile,
        workflow_security_rules=rules,
    )


def _resolve_plan_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(path, maximum=300)
        raise ConfigurationError(f"cannot resolve repository check plan {display}") from error


def _read_plan_document(path: Path) -> dict[str, object]:
    try:
        raw = read_file_prefix(path, maximum=MAX_REPOSITORY_PLAN_BYTES)
    except OSError as error:
        raise ConfigurationError(f"cannot read repository check plan {path}") from error
    if len(raw) > MAX_REPOSITORY_PLAN_BYTES:
        raise ConfigurationError(
            f"repository check plan exceeds {MAX_REPOSITORY_PLAN_BYTES} bytes: {path}"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"repository check plan is not valid UTF-8: {path}") from error
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(
            f"invalid repository check plan TOML in {path}: {error}"
        ) from error


def _plan_version(document: dict[str, object], *, path: Path) -> int:
    if "plan-version" not in document:
        raise ConfigurationError(f"repository check plan is missing plan-version in {path}")
    value = document["plan-version"]
    if isinstance(value, bool) or not isinstance(value, int) or value != 1:
        raise ConfigurationError(f"plan-version must be the integer 1 in {path}")
    return value


def _checks(document: dict[str, object], *, path: Path) -> tuple[RepositoryProvider, ...]:
    if "checks" not in document:
        raise ConfigurationError(f"repository check plan is missing checks in {path}")
    values = _string_array(
        document["checks"],
        key="checks",
        maximum=len(RepositoryProvider),
        allow_empty=False,
        path=path,
    )
    normalized: list[RepositoryProvider] = []
    for value in values:
        try:
            normalized.append(RepositoryProvider(value))
        except ValueError as error:
            display = safe_error_text(value, maximum=80)
            raise ConfigurationError(
                f"unknown repository check provider {display!r} in {path}"
            ) from error
    if len(set(normalized)) != len(normalized):
        raise ConfigurationError(f"repository check providers must not be repeated in {path}")
    selected = frozenset(normalized)
    return tuple(provider for provider in RepositoryProvider if provider in selected)


def _workflow_paths(value: object, *, path: Path) -> tuple[PurePosixPath, ...]:
    values = _string_array(
        value,
        key="workflow-paths",
        maximum=MAX_WORKFLOW_FILES,
        allow_empty=False,
        path=path,
    )
    normalized: list[PurePosixPath] = []
    seen: set[str] = set()
    for value in values:
        _validate_portable_workflow_path(value, path=path)
        folded = value.casefold()
        if folded in seen:
            raise ConfigurationError(f"workflow-paths contains duplicate path {value!r} in {path}")
        seen.add(folded)
        normalized.append(PurePosixPath(value))
    return tuple(normalized)


def _validate_portable_workflow_path(value: str, *, path: Path) -> None:
    posix = PurePosixPath(value)
    if (
        posix.as_posix() != value
        or "\\" in value
        or bool(PureWindowsPath(value).drive)
        or (posix.parts and posix.parts[0].startswith("~"))
    ):
        raise ConfigurationError(
            f"workflow-paths contains a non-portable repository-relative path in {path}"
        )
    try:
        validate_workflow_relative_path(value)
    except InputError as error:
        raise ConfigurationError(
            f"workflow-paths contains an invalid repository-relative path in {path}"
        ) from error
    for component in posix.parts:
        stem = component.split(".", 1)[0].casefold()
        if (
            component.endswith((" ", "."))
            or component != component.strip()
            or stem in _WINDOWS_RESERVED_PATH_NAMES
            or any(character in _WINDOWS_FORBIDDEN_PATH_CHARACTERS for character in component)
            or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in component)
        ):
            raise ConfigurationError(
                f"workflow-paths contains a non-portable repository-relative path in {path}"
            )
    try:
        if len(value.encode("utf-8")) > MAX_WORKFLOW_PATH_BYTES:
            raise ConfigurationError(
                f"workflow-paths contains an oversized repository-relative path in {path}"
            )
    except UnicodeEncodeError as error:  # pragma: no cover - also rejected by shared validation
        raise ConfigurationError(f"workflow-paths contains a non-UTF-8 path in {path}") from error


def _security_selection(
    document: dict[str, object],
    *,
    path: Path,
) -> tuple[WorkflowSecurityProfile | None, tuple[WorkflowSecurityRule, ...]]:
    has_profile = "workflow-security-profile" in document
    has_rules = "workflow-security-rules" in document
    if has_profile and has_rules:
        raise ConfigurationError(
            f"choose either workflow-security-profile or workflow-security-rules in {path}"
        )
    if not has_profile and not has_rules:
        return None, ()

    try:
        if has_profile:
            profile = document["workflow-security-profile"]
            if not isinstance(profile, str):
                raise ConfigurationError(f"workflow-security-profile must be a string in {path}")
            selection = normalize_security_rules(profile=profile)
            return selection.profile, ()

        rules = _string_array(
            document["workflow-security-rules"],
            key="workflow-security-rules",
            maximum=MAX_SECURITY_RULES,
            allow_empty=False,
            path=path,
        )
        selection = normalize_security_rules(rules=rules)
        return None, selection.rules
    except InputError as error:
        detail = safe_error_text(error, maximum=300)
        raise ConfigurationError(
            f"invalid workflow security selection in {path}: {detail}"
        ) from error


def _string_array(
    value: object,
    *,
    key: str,
    maximum: int,
    allow_empty: bool,
    path: Path,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{key} must be an array of strings in {path}")
    if not allow_empty and not value:
        raise ConfigurationError(f"{key} must not be empty in {path}")
    if len(value) > maximum:
        raise ConfigurationError(f"{key} exceeds {maximum} entries in {path}")
    return tuple(value)
