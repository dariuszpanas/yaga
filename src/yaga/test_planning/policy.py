"""Strict explicit TOML loading for the version-one qualification policy."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

from yaga.errors import ConfigurationError, safe_error_text
from yaga.files import read_regular_file_prefix
from yaga.test_planning.limits import MAX_POLICY_BYTES
from yaga.test_planning.models import (
    LoadedTestPolicy,
    Profile,
    QualificationPolicy,
    SelectionMode,
    SelectionRule,
    Suite,
    UnknownPathPolicy,
)

_POLICY_REQUIRED = frozenset({"test-policy-version", "suites", "profiles"})
_POLICY_KEYS = _POLICY_REQUIRED | {"unknown-paths", "full-on", "no-impact", "rules"}
_SUITE_REQUIRED = frozenset({"id", "binding"})
_SUITE_KEYS = _SUITE_REQUIRED | {"tags", "variants", "needs", "requires"}
_PROFILE_REQUIRED = frozenset({"mode"})
_PROFILE_KEYS = _PROFILE_REQUIRED | {"always"}
_RULE_REQUIRED = frozenset({"id", "when-any"})
_RULE_KEYS = _RULE_REQUIRED | {"select", "select-tags"}


def load_test_policy(path: Path) -> LoadedTestPolicy:
    """Read exactly one explicit regular file, preserving its exact-byte identity."""
    if not isinstance(path, Path):
        raise ConfigurationError("test policy path must be a Path")
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigurationError(
            f"cannot resolve test policy: {safe_error_text(path, maximum=300)}"
        ) from error
    try:
        raw = read_regular_file_prefix(resolved, maximum=MAX_POLICY_BYTES)
    except OSError as error:
        raise ConfigurationError(
            f"cannot read regular test policy: {safe_error_text(resolved, maximum=300)}"
        ) from error
    policy = parse_test_policy(raw)
    return LoadedTestPolicy(policy=policy, path=resolved, sha256=hashlib.sha256(raw).hexdigest())


def parse_test_policy(raw: bytes) -> QualificationPolicy:
    """Parse bounded UTF-8 TOML without filesystem, discovery, or environment access."""
    if type(raw) is not bytes:
        raise ConfigurationError("test policy must be bytes")
    if len(raw) > MAX_POLICY_BYTES:
        raise ConfigurationError(f"test policy exceeds {MAX_POLICY_BYTES} bytes")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigurationError("test policy must be valid UTF-8") from error
    try:
        document = tomllib.loads(text)
    except (ValueError, RecursionError) as error:
        raise ConfigurationError("invalid test policy TOML") from error
    try:
        return _parse_document(document)
    except ValueError as error:
        detail = safe_error_text(error, maximum=500)
        raise ConfigurationError(f"invalid test policy: {detail}") from error


def _parse_document(document: dict[str, object]) -> QualificationPolicy:
    _validate_keys(document, _POLICY_KEYS, _POLICY_REQUIRED, label="policy")
    version = document["test-policy-version"]
    if type(version) is not int or version != 1:
        raise ValueError("test-policy-version must be the integer 1")
    unknown_paths = _string(document.get("unknown-paths", "full"), label="unknown-paths")
    if unknown_paths not in {"full", "error"}:
        raise ValueError("unknown-paths must be full or error")
    suites = tuple(_parse_suite(table) for table in _tables(document["suites"], label="suites"))
    profiles_table = _table(document["profiles"], label="profiles")
    profiles = tuple(_parse_profile(name, value) for name, value in profiles_table.items())
    rules = tuple(_parse_rule(table) for table in _tables(document.get("rules", []), label="rules"))
    return QualificationPolicy(
        test_policy_version=version,
        suites=suites,
        profiles=profiles,
        rules=rules,
        unknown_paths=UnknownPathPolicy(unknown_paths),
        full_on=_strings(document.get("full-on", []), label="full-on"),
        no_impact=_strings(document.get("no-impact", []), label="no-impact"),
    )


def _parse_suite(table: dict[str, object]) -> Suite:
    _validate_keys(table, _SUITE_KEYS, _SUITE_REQUIRED, label="suite")
    return Suite(
        id=_string(table["id"], label="suite id"),
        binding=_string(table["binding"], label="suite binding"),
        tags=_strings(table.get("tags", []), label="suite tags"),
        variants=_strings(table.get("variants", ["default"]), label="suite variants"),
        needs=_strings(table.get("needs", []), label="suite needs"),
        requires=_strings(table.get("requires", []), label="suite requires"),
    )


def _parse_profile(name: str, value: object) -> Profile:
    table = _table(value, label="profile")
    _validate_keys(table, _PROFILE_KEYS, _PROFILE_REQUIRED, label="profile")
    mode = _string(table["mode"], label="profile mode")
    if mode not in {"selective", "full"}:
        raise ValueError("profile mode must be selective or full")
    return Profile(
        name=name,
        mode=SelectionMode(mode),
        always=_strings(table.get("always", []), label="profile always"),
    )


def _parse_rule(table: dict[str, object]) -> SelectionRule:
    _validate_keys(table, _RULE_KEYS, _RULE_REQUIRED, label="rule")
    return SelectionRule(
        id=_string(table["id"], label="rule id"),
        when_any=_strings(table["when-any"], label="rule when-any"),
        select=_strings(table.get("select", []), label="rule select"),
        select_tags=_strings(table.get("select-tags", []), label="rule select-tags"),
    )


def _validate_keys(
    table: dict[str, object], allowed: frozenset[str], required: frozenset[str], *, label: str
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        key = safe_error_text(unknown[0], maximum=100)
        raise ValueError(f"unknown {label} key {key!r} ({len(unknown)} unknown keys)")
    missing = sorted(required - set(table))
    if missing:
        raise ValueError(f"{label} is missing required keys: {', '.join(missing)}")


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _strings(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array of strings")
    return tuple(_string(item, label=label) for item in value)


def _table(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a table")
    return value


def _tables(value: object, *, label: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array of tables")
    return tuple(_table(item, label=label) for item in value)
