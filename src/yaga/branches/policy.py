"""Strict parsing for explicit branch-name policies."""

from __future__ import annotations

import tomllib
from pathlib import Path

from yaga.branches.models import MAX_BRANCH_PATTERNS, BranchPolicy, LoadedBranchPolicy
from yaga.errors import ConfigurationError, safe_error_text
from yaga.files import read_file_prefix

MAX_BRANCH_POLICY_BYTES = 64 * 1024

_POLICY_KEYS = frozenset({"branch-policy-version", "allowed-patterns"})


def load_branch_policy(path: Path) -> LoadedBranchPolicy:
    """Read and strictly normalize one explicitly selected TOML policy."""
    resolved = _resolve_policy_path(path)
    document = _read_policy_document(resolved)
    display_path = safe_error_text(resolved, maximum=300)

    unknown = sorted(set(document) - _POLICY_KEYS)
    if unknown:
        raise ConfigurationError(
            f"unknown branch policy key(s) in {display_path}: {', '.join(unknown)}"
        )
    missing = sorted(_POLICY_KEYS - set(document))
    if missing:
        raise ConfigurationError(
            f"branch policy is missing required key(s) in {display_path}: {', '.join(missing)}"
        )

    version = document["branch-policy-version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ConfigurationError(f"branch-policy-version must be the integer 1 in {display_path}")
    patterns = _parse_allowed_patterns(document["allowed-patterns"], path=display_path)
    try:
        policy = BranchPolicy(branch_policy_version=version, allowed_patterns=patterns)
    except ValueError as error:
        detail = safe_error_text(error, maximum=300)
        raise ConfigurationError(f"invalid branch policy in {display_path}: {detail}") from error
    return LoadedBranchPolicy(policy=policy, path=resolved)


def _resolve_policy_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(path, maximum=300)
        raise ConfigurationError(f"cannot resolve branch policy {display}") from error


def _read_policy_document(path: Path) -> dict[str, object]:
    display_path = safe_error_text(path, maximum=300)
    try:
        raw = read_file_prefix(path, maximum=MAX_BRANCH_POLICY_BYTES)
    except OSError as error:
        raise ConfigurationError(f"cannot read branch policy {display_path}") from error
    if len(raw) > MAX_BRANCH_POLICY_BYTES:
        raise ConfigurationError(
            f"branch policy exceeds {MAX_BRANCH_POLICY_BYTES} bytes: {display_path}"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"branch policy is not valid UTF-8: {display_path}") from error
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(
            f"invalid branch policy TOML in {display_path}: {error}"
        ) from error


def _parse_allowed_patterns(value: object, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"allowed-patterns must be an array of strings in {path}")
    if not value:
        raise ConfigurationError(f"allowed-patterns must not be empty in {path}")
    if len(value) > MAX_BRANCH_PATTERNS:
        raise ConfigurationError(
            f"allowed-patterns exceeds {MAX_BRANCH_PATTERNS} entries in {path}"
        )
    if len(set(value)) != len(value):
        raise ConfigurationError(f"allowed-patterns must be unique in {path}")
    return tuple(value)
