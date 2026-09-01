"""Strict TOML configuration discovery and parsing."""

from __future__ import annotations

import re
import tomllib
import unicodedata
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from yaga.commits.models import (
    BreakingMarkerPolicy,
    CasePolicy,
    CommitPolicy,
    EndingPolicy,
    LoadedConfig,
    MergePolicy,
    PresencePolicy,
)
from yaga.errors import ConfigurationError
from yaga.files import read_file_prefix

MAX_CONFIG_BYTES = 1024 * 1024
MAX_LIST_ITEMS = 128
MAX_TOKEN_LENGTH = 128
MAX_PATTERN_LENGTH = 256

_TYPE_TOKEN = re.compile(r"^[^\s()!:]+$")
_SCOPE_TOKEN = re.compile(r"^[^\s()]+$")
_ROOT_KEYS = frozenset({"config-version", "commit"})
_COMMIT_KEYS = frozenset(
    {
        "allowed-types",
        "type-case",
        "scope-policy",
        "allowed-scopes",
        "scope-case",
        "header-max-length",
        "description-min-length",
        "description-max-length",
        "description-ending",
        "body-policy",
        "body-min-length",
        "body-min-words",
        "body-max-line-length",
        "breaking-markers",
        "merge-commits",
        "ignored-headers",
        "max-commits",
    }
)


def load_config(
    explicit: Path | None = None,
    *,
    start: Path | None = None,
) -> LoadedConfig:
    """Load one explicit or nearest discovered YAGA configuration."""
    if explicit is not None:
        path = explicit.expanduser().resolve()
        document = _read_toml(path)
        root = _configuration_root(document, path=path, explicit=True)
        assert root is not None
        return LoadedConfig(policy=_parse_policy(root, path), path=path)

    start_path = (start or Path.cwd()).expanduser().resolve()
    if not start_path.is_dir():
        raise ConfigurationError(f"configuration start directory does not exist: {start_path}")
    for directory in _search_directories(start_path):
        standalone = directory / ".yaga.toml"
        if standalone.is_file():
            root = _configuration_root(_read_toml(standalone), path=standalone, explicit=True)
            assert root is not None
            return LoadedConfig(policy=_parse_policy(root, standalone), path=standalone)

        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            document = _read_toml(pyproject)
            root = _configuration_root(document, path=pyproject, explicit=False)
            if root is not None:
                return LoadedConfig(policy=_parse_policy(root, pyproject), path=pyproject)
    return LoadedConfig(policy=CommitPolicy(), path=None)


def _search_directories(start: Path) -> list[Path]:
    directories: list[Path] = []
    current = start
    while True:
        directories.append(current)
        if (current / ".git").exists() or current.parent == current:
            return directories
        current = current.parent


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        raw = read_file_prefix(path, maximum=MAX_CONFIG_BYTES)
    except OSError as error:
        raise ConfigurationError(f"cannot read configuration {path}: {error}") from error
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigurationError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes: {path}")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"configuration is not valid UTF-8: {path}") from error
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"invalid TOML in {path}: {error}") from error


def _configuration_root(
    document: Mapping[str, Any],
    *,
    path: Path,
    explicit: bool,
) -> Mapping[str, Any] | None:
    if path.name == "pyproject.toml":
        tool = document.get("tool")
        if tool is None:
            if explicit:
                raise ConfigurationError(f"{path} has no [tool.yaga] table")
            return None
        if not isinstance(tool, dict):
            raise ConfigurationError(f"[tool] must be a table in {path}")
        root = tool.get("yaga")
        if root is None:
            if explicit:
                raise ConfigurationError(f"{path} has no [tool.yaga] table")
            return None
    else:
        root = document
    if not isinstance(root, dict):
        raise ConfigurationError(f"YAGA configuration must be a table in {path}")
    return root


def _parse_policy(root: Mapping[str, Any], path: Path) -> CommitPolicy:
    _reject_unknown(root, _ROOT_KEYS, label="YAGA", path=path)
    config_version = _integer(root.get("config-version", 1), "config-version", 1, 1, path)
    raw_commit = root.get("commit", {})
    if not isinstance(raw_commit, dict):
        raise ConfigurationError(f"commit must be a table in {path}")
    _reject_unknown(raw_commit, _COMMIT_KEYS, label="commit", path=path)

    allowed_types = _optional_tokens(
        raw_commit,
        "allowed-types",
        token_pattern=_TYPE_TOKEN,
        allow_empty=False,
        path=path,
    )
    allowed_scopes = _optional_tokens(
        raw_commit,
        "allowed-scopes",
        token_pattern=_SCOPE_TOKEN,
        allow_empty=True,
        path=path,
    )
    scope_policy = _enum(
        raw_commit.get("scope-policy", PresencePolicy.OPTIONAL.value),
        PresencePolicy,
        "scope-policy",
        path,
    )
    if scope_policy is PresencePolicy.FORBIDDEN and allowed_scopes not in (None, ()):
        raise ConfigurationError(
            f"allowed-scopes cannot contain values when scope-policy is forbidden in {path}"
        )
    if scope_policy is PresencePolicy.REQUIRED and allowed_scopes == ():
        raise ConfigurationError(
            f"allowed-scopes cannot be empty when scope-policy is required in {path}"
        )

    description_min = _integer(
        raw_commit.get("description-min-length", 1),
        "description-min-length",
        1,
        10_000,
        path,
    )
    description_max = _optional_integer(
        raw_commit, "description-max-length", minimum=1, maximum=100_000, path=path
    )
    if description_max is not None and description_max < description_min:
        raise ConfigurationError(
            f"description-max-length is smaller than description-min-length in {path}"
        )
    body_policy = _enum(
        raw_commit.get("body-policy", PresencePolicy.OPTIONAL.value),
        PresencePolicy,
        "body-policy",
        path,
    )
    body_min = _integer(
        raw_commit.get("body-min-length", 0),
        "body-min-length",
        0,
        100_000,
        path,
    )
    body_min_words = _integer(
        raw_commit.get("body-min-words", 0),
        "body-min-words",
        0,
        100_000,
        path,
    )
    if body_policy is PresencePolicy.FORBIDDEN and (body_min or body_min_words):
        incompatible = " and ".join(
            key
            for key, value in (
                ("body-min-length", body_min),
                ("body-min-words", body_min_words),
            )
            if value
        )
        raise ConfigurationError(
            f"{incompatible} must be zero when body-policy is forbidden in {path}"
        )

    header_max = _optional_integer(
        raw_commit, "header-max-length", minimum=1, maximum=100_000, path=path
    )
    minimum_header = _minimum_header_length(
        allowed_types=allowed_types,
        scope_policy=scope_policy,
        allowed_scopes=allowed_scopes,
        description_min=description_min,
    )
    if header_max is not None and header_max < minimum_header:
        raise ConfigurationError(
            f"header-max-length cannot fit the configured minimum header length of "
            f"{minimum_header} in {path}"
        )

    return CommitPolicy(
        config_version=config_version,
        allowed_types=allowed_types,
        type_case=_enum(
            raw_commit.get("type-case", CasePolicy.ANY.value),
            CasePolicy,
            "type-case",
            path,
        ),
        scope_policy=scope_policy,
        allowed_scopes=allowed_scopes,
        scope_case=_enum(
            raw_commit.get("scope-case", CasePolicy.ANY.value),
            CasePolicy,
            "scope-case",
            path,
        ),
        header_max_length=header_max,
        description_min_length=description_min,
        description_max_length=description_max,
        description_ending=_enum(
            raw_commit.get("description-ending", EndingPolicy.ALLOW.value),
            EndingPolicy,
            "description-ending",
            path,
        ),
        body_policy=body_policy,
        body_min_length=body_min,
        body_min_words=body_min_words,
        body_max_line_length=_optional_integer(
            raw_commit, "body-max-line-length", minimum=1, maximum=100_000, path=path
        ),
        breaking_markers=_enum(
            raw_commit.get("breaking-markers", BreakingMarkerPolicy.EITHER.value),
            BreakingMarkerPolicy,
            "breaking-markers",
            path,
        ),
        merge_commits=_enum(
            raw_commit.get("merge-commits", MergePolicy.IGNORE.value),
            MergePolicy,
            "merge-commits",
            path,
        ),
        ignored_headers=_string_list(
            raw_commit.get("ignored-headers", []),
            "ignored-headers",
            path,
            allow_empty=True,
            max_item_length=MAX_PATTERN_LENGTH,
        ),
        max_commits=_integer(raw_commit.get("max-commits", 256), "max-commits", 1, 10_000, path),
    )


def _reject_unknown(
    table: Mapping[str, Any], allowed: frozenset[str], *, label: str, path: Path
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        names = ", ".join(unknown)
        raise ConfigurationError(f"unknown {label} configuration key(s) in {path}: {names}")


def _optional_tokens(
    table: Mapping[str, Any],
    key: str,
    *,
    token_pattern: re.Pattern[str],
    allow_empty: bool,
    path: Path,
) -> tuple[str, ...] | None:
    if key not in table:
        return None
    values = _string_list(
        table[key], key, path, allow_empty=allow_empty, max_item_length=MAX_TOKEN_LENGTH
    )
    normalized: set[str] = set()
    for value in values:
        if token_pattern.fullmatch(value) is None:
            raise ConfigurationError(f"{key} contains invalid token {value!r} in {path}")
        folded = value.casefold()
        if folded in normalized:
            raise ConfigurationError(f"{key} contains duplicate token {value!r} in {path}")
        normalized.add(folded)
    return values


def _string_list(
    value: object,
    key: str,
    path: Path,
    *,
    allow_empty: bool,
    max_item_length: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{key} must be an array of strings in {path}")
    if not allow_empty and not value:
        raise ConfigurationError(f"{key} must not be empty in {path}")
    if len(value) > MAX_LIST_ITEMS:
        raise ConfigurationError(f"{key} exceeds {MAX_LIST_ITEMS} entries in {path}")
    for item in value:
        if not item or len(item) > max_item_length or any(_unsafe_character(char) for char in item):
            raise ConfigurationError(
                f"{key} contains an empty, oversized, or unsafe value in {path}"
            )
    return tuple(value)


def _minimum_header_length(
    *,
    allowed_types: tuple[str, ...] | None,
    scope_policy: PresencePolicy,
    allowed_scopes: tuple[str, ...] | None,
    description_min: int,
) -> int:
    shortest_type = min(map(len, allowed_types)) if allowed_types is not None else 1
    scope_length = 0
    if scope_policy is PresencePolicy.REQUIRED:
        shortest_scope = min(map(len, allowed_scopes)) if allowed_scopes is not None else 1
        scope_length = shortest_scope + 2
    return shortest_type + scope_length + 2 + description_min


def _unsafe_character(value: str) -> bool:
    return unicodedata.category(value) in {"Cc", "Cf", "Cs", "Zl", "Zp"}


def _integer(value: object, key: str, minimum: int, maximum: int, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{key} must be an integer from {minimum} through {maximum} in {path}"
        )
    return value


def _optional_integer(
    table: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
    path: Path,
) -> int | None:
    if key not in table:
        return None
    return _integer(table[key], key, minimum, maximum, path)


def _enum[EnumValue: StrEnum](
    value: object,
    enum_type: type[EnumValue],
    key: str,
    path: Path,
) -> EnumValue:
    if not isinstance(value, str):
        raise ConfigurationError(f"{key} must be a string in {path}")
    try:
        return enum_type(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in enum_type)
        raise ConfigurationError(f"{key} must be one of {allowed} in {path}") from error
