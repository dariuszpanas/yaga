"""Strict parsing for explicit committed Git-mode policies."""

from __future__ import annotations

import tomllib
from pathlib import Path

from yaga.errors import ConfigurationError, safe_error_text
from yaga.files import read_file_prefix
from yaga.modes.models import (
    MAX_MODE_POLICY_OVERRIDES,
    LoadedModePolicy,
    ModeKind,
    ModePathOverride,
    ModePolicy,
)

MAX_MODE_POLICY_BYTES = 64 * 1024

_POLICY_KEYS = frozenset({"mode-policy-version", "default-allowed-modes", "path-overrides"})
_REQUIRED_POLICY_KEYS = frozenset({"mode-policy-version", "default-allowed-modes"})
_PATH_OVERRIDE_KEYS = frozenset({"pattern", "allowed-modes"})
_MODE_BY_NAME = {kind.value: kind for kind in ModeKind}


def load_mode_policy(path: Path) -> LoadedModePolicy:
    """Read and strictly normalize one explicitly selected TOML policy."""
    resolved = _resolve_policy_path(path)
    document = _read_policy_document(resolved)
    display_path = safe_error_text(resolved, maximum=300)

    unknown = sorted(set(document) - _POLICY_KEYS)
    if unknown:
        raise ConfigurationError(
            f"unknown mode policy key(s) in {display_path}: {', '.join(unknown)}"
        )
    missing = sorted(_REQUIRED_POLICY_KEYS - set(document))
    if missing:
        raise ConfigurationError(
            f"mode policy is missing required key(s) in {display_path}: {', '.join(missing)}"
        )
    version = document["mode-policy-version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ConfigurationError(f"mode-policy-version must be the integer 1 in {display_path}")
    default_allowed_modes = _parse_allowed_modes(
        document["default-allowed-modes"],
        label="default-allowed-modes",
        path=display_path,
    )
    path_overrides = _parse_path_overrides(
        document.get("path-overrides", []),
        path=display_path,
    )
    try:
        policy = ModePolicy(
            mode_policy_version=version,
            default_allowed_modes=default_allowed_modes,
            path_overrides=path_overrides,
        )
    except ValueError as error:
        detail = safe_error_text(error, maximum=300)
        raise ConfigurationError(f"invalid mode policy in {display_path}: {detail}") from error
    return LoadedModePolicy(policy=policy, path=resolved)


def _resolve_policy_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(path, maximum=300)
        raise ConfigurationError(f"cannot resolve mode policy {display}") from error


def _read_policy_document(path: Path) -> dict[str, object]:
    display_path = safe_error_text(path, maximum=300)
    try:
        raw = read_file_prefix(path, maximum=MAX_MODE_POLICY_BYTES)
    except OSError as error:
        raise ConfigurationError(f"cannot read mode policy {display_path}") from error
    if len(raw) > MAX_MODE_POLICY_BYTES:
        raise ConfigurationError(
            f"mode policy exceeds {MAX_MODE_POLICY_BYTES} bytes: {display_path}"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"mode policy is not valid UTF-8: {display_path}") from error
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"invalid mode policy TOML in {display_path}: {error}") from error
    if not isinstance(document, dict):  # pragma: no cover - tomllib's documented shape
        raise ConfigurationError(f"mode policy root must be a table in {display_path}")
    return document


def _parse_allowed_modes(
    value: object,
    *,
    label: str,
    path: str,
) -> tuple[ModeKind, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{label} must be a nonempty array of mode names in {path}")
    if any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{label} must contain only strings in {path}")
    if len(set(value)) != len(value):
        raise ConfigurationError(f"{label} must be unique in {path}")
    unknown = tuple(item for item in value if item not in _MODE_BY_NAME)
    if unknown:
        raise ConfigurationError(f"{label} contains unknown mode {unknown[0]!r} in {path}")
    selected = set(value)
    return tuple(kind for kind in ModeKind if kind.value in selected)


def _parse_path_overrides(
    value: object,
    *,
    path: str,
) -> tuple[ModePathOverride, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"path-overrides must be an array of tables in {path}")
    if len(value) > MAX_MODE_POLICY_OVERRIDES:
        raise ConfigurationError(
            f"path-overrides exceeds {MAX_MODE_POLICY_OVERRIDES} entries in {path}"
        )
    parsed: list[ModePathOverride] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        label = f"path-overrides entry {index}"
        if not isinstance(item, dict):
            raise ConfigurationError(f"{label} must be a table in {path}")
        unknown = sorted(set(item) - _PATH_OVERRIDE_KEYS)
        if unknown:
            raise ConfigurationError(f"unknown {label} key(s) in {path}: {', '.join(unknown)}")
        missing = sorted(_PATH_OVERRIDE_KEYS - set(item))
        if missing:
            raise ConfigurationError(
                f"{label} is missing required key(s) in {path}: {', '.join(missing)}"
            )
        pattern = item["pattern"]
        if not isinstance(pattern, str):
            raise ConfigurationError(f"{label} pattern must be a string in {path}")
        if pattern in seen:
            raise ConfigurationError(
                f"path-overrides patterns must be unique and case-sensitive in {path}"
            )
        seen.add(pattern)
        allowed_modes = _parse_allowed_modes(
            item["allowed-modes"],
            label=f"{label} allowed-modes",
            path=path,
        )
        try:
            parsed.append(ModePathOverride(pattern, allowed_modes))
        except ValueError as error:
            detail = safe_error_text(error, maximum=300)
            raise ConfigurationError(f"invalid {label} in {path}: {detail}") from error
    return tuple(parsed)
