"""Strict parsing for explicit committed blob-size policies."""

from __future__ import annotations

import tomllib
from pathlib import Path

from yaga.errors import ConfigurationError, safe_error_text
from yaga.files import read_file_prefix
from yaga.sizes.models import (
    MAX_SIZE_BYTES,
    MAX_SIZE_POLICY_ENTRIES,
    LoadedSizePolicy,
    SizePathLimit,
    SizePolicy,
)

MAX_SIZE_POLICY_BYTES = 64 * 1024

_POLICY_KEYS = frozenset(
    {
        "size-policy-version",
        "default-max-blob-bytes",
        "max-total-blob-bytes",
        "path-limits",
    }
)
_REQUIRED_POLICY_KEYS = frozenset({"size-policy-version", "default-max-blob-bytes"})
_PATH_LIMIT_KEYS = frozenset({"pattern", "max-blob-bytes"})


def load_size_policy(path: Path) -> LoadedSizePolicy:
    """Read and strictly normalize one explicitly selected TOML policy."""
    resolved = _resolve_policy_path(path)
    document = _read_policy_document(resolved)
    display_path = safe_error_text(resolved, maximum=300)

    unknown = sorted(set(document) - _POLICY_KEYS)
    if unknown:
        raise ConfigurationError(
            f"unknown size policy key(s) in {display_path}: {', '.join(unknown)}"
        )
    missing = sorted(_REQUIRED_POLICY_KEYS - set(document))
    if missing:
        raise ConfigurationError(
            f"size policy is missing required key(s) in {display_path}: {', '.join(missing)}"
        )

    version = document["size-policy-version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ConfigurationError(f"size-policy-version must be the integer 1 in {display_path}")
    default_limit = _parse_byte_integer(
        document["default-max-blob-bytes"],
        key="default-max-blob-bytes",
        path=display_path,
    )
    total_limit = None
    if "max-total-blob-bytes" in document:
        total_limit = _parse_byte_integer(
            document["max-total-blob-bytes"],
            key="max-total-blob-bytes",
            path=display_path,
        )
    path_limits = _parse_path_limits(document.get("path-limits", []), path=display_path)

    try:
        policy = SizePolicy(
            size_policy_version=version,
            default_max_blob_bytes=default_limit,
            max_total_blob_bytes=total_limit,
            path_limits=path_limits,
        )
    except ValueError as error:
        detail = safe_error_text(error, maximum=300)
        raise ConfigurationError(f"invalid size policy in {display_path}: {detail}") from error
    return LoadedSizePolicy(policy=policy, path=resolved)


def _resolve_policy_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(path, maximum=300)
        raise ConfigurationError(f"cannot resolve size policy {display}") from error


def _read_policy_document(path: Path) -> dict[str, object]:
    display_path = safe_error_text(path, maximum=300)
    try:
        raw = read_file_prefix(path, maximum=MAX_SIZE_POLICY_BYTES)
    except OSError as error:
        raise ConfigurationError(f"cannot read size policy {display_path}") from error
    if len(raw) > MAX_SIZE_POLICY_BYTES:
        raise ConfigurationError(
            f"size policy exceeds {MAX_SIZE_POLICY_BYTES} bytes: {display_path}"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"size policy is not valid UTF-8: {display_path}") from error
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"invalid size policy TOML in {display_path}: {error}") from error
    if not isinstance(document, dict):  # pragma: no cover - tomllib's documented shape
        raise ConfigurationError(f"size policy root must be a table in {display_path}")
    return document


def _parse_byte_integer(value: object, *, key: str, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SIZE_BYTES:
        raise ConfigurationError(
            f"{key} must be an integer from 0 through {MAX_SIZE_BYTES} in {path}"
        )
    return value


def _parse_path_limits(value: object, *, path: str) -> tuple[SizePathLimit, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"path-limits must be an array of tables in {path}")
    if len(value) > MAX_SIZE_POLICY_ENTRIES:
        raise ConfigurationError(f"path-limits exceeds {MAX_SIZE_POLICY_ENTRIES} entries in {path}")

    parsed: list[SizePathLimit] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        label = f"path-limits entry {index}"
        if not isinstance(item, dict):
            raise ConfigurationError(f"{label} must be a table in {path}")
        unknown = sorted(set(item) - _PATH_LIMIT_KEYS)
        if unknown:
            raise ConfigurationError(f"unknown {label} key(s) in {path}: {', '.join(unknown)}")
        missing = sorted(_PATH_LIMIT_KEYS - set(item))
        if missing:
            raise ConfigurationError(
                f"{label} is missing required key(s) in {path}: {', '.join(missing)}"
            )
        pattern = item["pattern"]
        if not isinstance(pattern, str):
            raise ConfigurationError(f"{label} pattern must be a string in {path}")
        if pattern in seen:
            raise ConfigurationError(
                f"path-limits patterns must be unique and case-sensitive in {path}"
            )
        seen.add(pattern)
        limit = _parse_byte_integer(
            item["max-blob-bytes"],
            key=f"{label} max-blob-bytes",
            path=path,
        )
        try:
            parsed.append(SizePathLimit(pattern=pattern, max_blob_bytes=limit))
        except ValueError as error:
            detail = safe_error_text(error, maximum=300)
            raise ConfigurationError(f"invalid {label} in {path}: {detail}") from error
    return tuple(parsed)
