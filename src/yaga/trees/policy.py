"""Strict parsing for explicit tracked-tree policies."""

from __future__ import annotations

import tomllib
from pathlib import Path

from yaga.errors import ConfigurationError, safe_error_text
from yaga.files import read_file_prefix
from yaga.trees.models import (
    MAX_TREE_POLICY_ENTRIES,
    LoadedTreePolicy,
    TreePolicy,
)

MAX_TREE_POLICY_BYTES = 64 * 1024

_POLICY_KEYS = frozenset({"tree-policy-version", "required-paths", "forbidden-patterns"})


def load_tree_policy(path: Path) -> LoadedTreePolicy:
    """Read and strictly normalize one explicitly selected TOML policy."""
    resolved = _resolve_policy_path(path)
    document = _read_policy_document(resolved)
    display_path = safe_error_text(resolved, maximum=300)

    unknown = sorted(set(document) - _POLICY_KEYS)
    if unknown:
        raise ConfigurationError(
            f"unknown tree policy key(s) in {display_path}: {', '.join(unknown)}"
        )
    missing = sorted(_POLICY_KEYS - set(document))
    if missing:
        raise ConfigurationError(
            f"tree policy is missing required key(s) in {display_path}: {', '.join(missing)}"
        )

    version = document["tree-policy-version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ConfigurationError(f"tree-policy-version must be the integer 1 in {display_path}")
    required_paths = _parse_string_array(
        document["required-paths"],
        key="required-paths",
        path=display_path,
    )
    forbidden_patterns = _parse_string_array(
        document["forbidden-patterns"],
        key="forbidden-patterns",
        path=display_path,
    )
    total = len(required_paths) + len(forbidden_patterns)
    if not 1 <= total <= MAX_TREE_POLICY_ENTRIES:
        raise ConfigurationError(
            "required-paths and forbidden-patterns must contain 1 through "
            f"{MAX_TREE_POLICY_ENTRIES} combined entries in {display_path}"
        )

    try:
        policy = TreePolicy(
            tree_policy_version=version,
            required_paths=required_paths,
            forbidden_patterns=forbidden_patterns,
        )
    except ValueError as error:
        detail = safe_error_text(error, maximum=300)
        raise ConfigurationError(f"invalid tree policy in {display_path}: {detail}") from error
    return LoadedTreePolicy(policy=policy, path=resolved)


def _resolve_policy_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(path, maximum=300)
        raise ConfigurationError(f"cannot resolve tree policy {display}") from error


def _read_policy_document(path: Path) -> dict[str, object]:
    display_path = safe_error_text(path, maximum=300)
    try:
        raw = read_file_prefix(path, maximum=MAX_TREE_POLICY_BYTES)
    except OSError as error:
        raise ConfigurationError(f"cannot read tree policy {display_path}") from error
    if len(raw) > MAX_TREE_POLICY_BYTES:
        raise ConfigurationError(
            f"tree policy exceeds {MAX_TREE_POLICY_BYTES} bytes: {display_path}"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"tree policy is not valid UTF-8: {display_path}") from error
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"invalid tree policy TOML in {display_path}: {error}") from error
    if not isinstance(document, dict):  # pragma: no cover - tomllib's documented shape
        raise ConfigurationError(f"tree policy root must be a table in {display_path}")
    return document


def _parse_string_array(value: object, *, key: str, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{key} must be an array of strings in {path}")
    if len(value) > MAX_TREE_POLICY_ENTRIES:
        raise ConfigurationError(f"{key} exceeds {MAX_TREE_POLICY_ENTRIES} entries in {path}")
    if len(set(value)) != len(value):
        raise ConfigurationError(f"{key} must contain unique case-sensitive values in {path}")
    return tuple(value)
