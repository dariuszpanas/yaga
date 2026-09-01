"""Strict parsing for explicit committed-path portability policies."""

from __future__ import annotations

import tomllib
from pathlib import Path

from yaga.errors import ConfigurationError, safe_error_text
from yaga.files import read_file_prefix
from yaga.paths.models import (
    MAX_PATH_POLICY_RULES,
    PATH_RULES,
    WINDOWS_COMPATIBLE_V1_PROFILE,
    WINDOWS_COMPATIBLE_V1_RULES,
    LoadedPathPolicy,
    PathPolicy,
)

MAX_PATH_POLICY_BYTES = 64 * 1024

_POLICY_KEYS = frozenset({"path-policy-version", "profile", "rules"})
_SUPPORTED_PROFILES = frozenset({WINDOWS_COMPATIBLE_V1_PROFILE})


def load_path_policy(path: Path) -> LoadedPathPolicy:
    """Read and strictly normalize one explicitly selected TOML policy."""
    resolved = _resolve_policy_path(path)
    document = _read_policy_document(resolved)
    display_path = safe_error_text(resolved, maximum=300)

    unknown = sorted(set(document) - _POLICY_KEYS)
    if unknown:
        raise ConfigurationError(
            f"unknown path policy key(s) in {display_path}: {', '.join(unknown)}"
        )
    if "path-policy-version" not in document:
        raise ConfigurationError(
            f"path policy is missing required key path-policy-version in {display_path}"
        )
    version = document["path-policy-version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ConfigurationError(f"path-policy-version must be the integer 1 in {display_path}")

    selectors = {key for key in ("profile", "rules") if key in document}
    if len(selectors) != 1:
        raise ConfigurationError(
            f"path policy must define exactly one of profile or rules in {display_path}"
        )

    profile: str | None = None
    if "profile" in selectors:
        value = document["profile"]
        if not isinstance(value, str) or value not in _SUPPORTED_PROFILES:
            raise ConfigurationError(
                f"profile must be {WINDOWS_COMPATIBLE_V1_PROFILE!r} in {display_path}"
            )
        profile = value
        rules = WINDOWS_COMPATIBLE_V1_RULES
    else:
        rules = _parse_rules(document["rules"], path=display_path)

    try:
        policy = PathPolicy(
            path_policy_version=version,
            rules=rules,
            profile=profile,
        )
    except ValueError as error:
        detail = safe_error_text(error, maximum=300)
        raise ConfigurationError(f"invalid path policy in {display_path}: {detail}") from error
    return LoadedPathPolicy(policy=policy, path=resolved)


def _resolve_policy_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(path, maximum=300)
        raise ConfigurationError(f"cannot resolve path policy {display}") from error


def _read_policy_document(path: Path) -> dict[str, object]:
    display_path = safe_error_text(path, maximum=300)
    try:
        raw = read_file_prefix(path, maximum=MAX_PATH_POLICY_BYTES)
    except OSError as error:
        raise ConfigurationError(f"cannot read path policy {display_path}") from error
    if len(raw) > MAX_PATH_POLICY_BYTES:
        raise ConfigurationError(
            f"path policy exceeds {MAX_PATH_POLICY_BYTES} bytes: {display_path}"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"path policy is not valid UTF-8: {display_path}") from error
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"invalid path policy TOML in {display_path}: {error}") from error
    if not isinstance(document, dict):  # pragma: no cover - tomllib's documented shape
        raise ConfigurationError(f"path policy root must be a table in {display_path}")
    return document


def _parse_rules(value: object, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(rule, str) for rule in value):
        raise ConfigurationError(f"rules must be an array of strings in {path}")
    if not 1 <= len(value) <= MAX_PATH_POLICY_RULES:
        raise ConfigurationError(
            f"rules must contain 1 through {MAX_PATH_POLICY_RULES} entries in {path}"
        )
    if len(set(value)) != len(value):
        raise ConfigurationError(f"rules must contain unique case-sensitive values in {path}")
    unknown = tuple(rule for rule in value if rule not in PATH_RULES)
    if unknown:
        raise ConfigurationError(f"unknown path policy rule in {path}: {unknown[0]}")
    return tuple(value)
