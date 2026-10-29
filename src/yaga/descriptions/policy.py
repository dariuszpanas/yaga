"""Strict bounded loading of one explicitly selected description policy."""

from __future__ import annotations

import tomllib
from pathlib import Path

from yaga.descriptions.models import DescriptionPolicy, LoadedDescriptionPolicy
from yaga.errors import ConfigurationError, safe_error_text
from yaga.files import read_regular_file_prefix

MAX_DESCRIPTION_POLICY_BYTES = 64 * 1024
_POLICY_KEYS = frozenset(
    {
        "description-policy-version",
        "min-words",
        "max-length",
        "placeholders",
        "require-checked-tasks",
        "typos",
        "heading-min-level",
        "heading-max-length",
    }
)


def load_description_policy(path: Path) -> LoadedDescriptionPolicy:
    """Load one closed versioned TOML document without policy discovery."""
    try:
        resolved = path.expanduser().resolve()
        raw = read_regular_file_prefix(resolved, maximum=MAX_DESCRIPTION_POLICY_BYTES)
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigurationError(
            "description policy could not be read as a regular file"
        ) from error
    if len(raw) > MAX_DESCRIPTION_POLICY_BYTES:
        raise ConfigurationError("description policy exceeds the 65536-byte limit")
    try:
        document = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as error:
        raise ConfigurationError("description policy is not valid UTF-8 TOML") from error
    unknown = sorted(set(document) - _POLICY_KEYS)
    if unknown:
        detail = safe_error_text(", ".join(unknown), maximum=300)
        raise ConfigurationError(f"unknown description policy key(s): {detail}")
    if "description-policy-version" not in document:
        raise ConfigurationError("description policy requires description-policy-version")
    placeholders = document.get("placeholders", ["TODO", "TBD"])
    if not isinstance(placeholders, list):
        raise ConfigurationError("description policy placeholders must be an array of strings")
    try:
        policy = DescriptionPolicy(
            description_policy_version=document["description-policy-version"],
            min_words=document.get("min-words", 1),
            max_length=document.get("max-length", 65536),
            placeholders=tuple(placeholders),
            require_checked_tasks=document.get("require-checked-tasks", False),
            typos=document.get("typos", "skip"),
            heading_min_level=document.get("heading-min-level", 2),
            heading_max_length=document.get("heading-max-length", 120),
        )
    except ValueError as error:
        raise ConfigurationError(f"invalid description policy: {safe_error_text(error)}") from error
    return LoadedDescriptionPolicy(policy=policy, path=resolved)
