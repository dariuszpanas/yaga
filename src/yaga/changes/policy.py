"""Strict parsing for explicit changed-path coupling policies."""

from __future__ import annotations

import tomllib
from pathlib import Path

from yaga.changes.models import (
    MAX_CHANGE_PATTERNS,
    MAX_CHANGE_RULE_NAME_CHARS,
    MAX_CHANGE_RULES,
    ChangePolicy,
    ChangeRule,
    LoadedChangePolicy,
)
from yaga.errors import ConfigurationError, safe_error_text
from yaga.files import read_file_prefix

MAX_CHANGE_POLICY_BYTES = 64 * 1024

_POLICY_KEYS = frozenset({"change-policy-version", "rules"})
_RULE_KEYS = frozenset({"name", "when-any", "require-any"})


def load_change_policy(path: Path) -> LoadedChangePolicy:
    """Read and strictly normalize one explicitly selected TOML policy."""
    resolved = _resolve_policy_path(path)
    document = _read_policy_document(resolved)
    display_path = safe_error_text(resolved, maximum=300)

    _reject_unknown(document, _POLICY_KEYS, label="change policy", path=display_path)
    if "change-policy-version" not in document:
        raise ConfigurationError(
            f"change policy is missing change-policy-version in {display_path}"
        )
    version = document["change-policy-version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ConfigurationError(f"change-policy-version must be the integer 1 in {display_path}")
    if "rules" not in document:
        raise ConfigurationError(f"change policy is missing rules in {display_path}")

    rules = _parse_rules(document["rules"], path=display_path)
    return LoadedChangePolicy(
        policy=ChangePolicy(change_policy_version=version, rules=rules),
        path=resolved,
    )


def _resolve_policy_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        display = safe_error_text(path, maximum=300)
        raise ConfigurationError(f"cannot resolve change policy {display}") from error


def _read_policy_document(path: Path) -> dict[str, object]:
    display_path = safe_error_text(path, maximum=300)
    try:
        raw = read_file_prefix(path, maximum=MAX_CHANGE_POLICY_BYTES)
    except OSError as error:
        raise ConfigurationError(f"cannot read change policy {display_path}") from error
    if len(raw) > MAX_CHANGE_POLICY_BYTES:
        raise ConfigurationError(
            f"change policy exceeds {MAX_CHANGE_POLICY_BYTES} bytes: {display_path}"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"change policy is not valid UTF-8: {display_path}") from error
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(
            f"invalid change policy TOML in {display_path}: {error}"
        ) from error


def _parse_rules(value: object, *, path: str) -> tuple[ChangeRule, ...]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ConfigurationError(f"rules must be an array of tables in {path}")
    if not value:
        raise ConfigurationError(f"rules must not be empty in {path}")
    if len(value) > MAX_CHANGE_RULES:
        raise ConfigurationError(f"rules exceeds {MAX_CHANGE_RULES} entries in {path}")

    rules: list[ChangeRule] = []
    names: set[str] = set()
    for index, raw_rule in enumerate(value, start=1):
        _reject_unknown(raw_rule, _RULE_KEYS, label=f"rules[{index}]", path=path)
        missing = sorted(_RULE_KEYS - set(raw_rule))
        if missing:
            raise ConfigurationError(
                f"rules[{index}] is missing required key(s) in {path}: {', '.join(missing)}"
            )
        name = raw_rule["name"]
        if not isinstance(name, str):
            raise ConfigurationError(f"rules[{index}].name must be a string in {path}")
        when_any = _patterns(raw_rule["when-any"], key=f"rules[{index}].when-any", path=path)
        require_any = _patterns(
            raw_rule["require-any"],
            key=f"rules[{index}].require-any",
            path=path,
        )
        try:
            rule = ChangeRule(name=name, when_any=when_any, require_any=require_any)
        except ValueError as error:
            if not _looks_like_rule_name(name):
                raise ConfigurationError(
                    f"rules[{index}].name must be a 1 through {MAX_CHANGE_RULE_NAME_CHARS} "
                    f"character lowercase ASCII slug in {path}"
                ) from error
            detail = safe_error_text(error, maximum=300)
            raise ConfigurationError(f"invalid rules[{index}] in {path}: {detail}") from error
        if rule.name in names:
            raise ConfigurationError(f"rules contains duplicate name {rule.name!r} in {path}")
        names.add(rule.name)
        rules.append(rule)
    return tuple(rules)


def _patterns(value: object, *, key: str, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{key} must be an array of strings in {path}")
    if not value:
        raise ConfigurationError(f"{key} must not be empty in {path}")
    if len(value) > MAX_CHANGE_PATTERNS:
        raise ConfigurationError(f"{key} exceeds {MAX_CHANGE_PATTERNS} entries in {path}")
    if len(set(value)) != len(value):
        raise ConfigurationError(f"{key} patterns must be unique in {path}")
    return tuple(value)


def _reject_unknown(
    table: dict[str, object],
    allowed: frozenset[str],
    *,
    label: str,
    path: str,
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigurationError(f"unknown {label} key(s) in {path}: {', '.join(unknown)}")


def _looks_like_rule_name(value: str) -> bool:
    if not 1 <= len(value) <= MAX_CHANGE_RULE_NAME_CHARS:
        return False
    if value[0] == "-" or value[-1] == "-" or "--" in value:
        return False
    return all(
        "a" <= character <= "z" or "0" <= character <= "9" or character == "-"
        for character in value
    )
