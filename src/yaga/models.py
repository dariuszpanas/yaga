"""Strict, bounded models and parsers shared by every gate."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from yaga.errors import GateError

MAX_GITHUB_ID = 9_223_372_036_854_775_807
MAX_REF_BYTES = 255
MAX_LOGIN_BYTES = 128
MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_REQUEST_TIMEOUT_SECONDS = 30.0

REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
WORKFLOW_PATH_RE = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]{1,128}\.ya?ml\Z")


def record(value: object, label: str) -> dict[str, Any]:
    """Require one JSON object."""
    if not isinstance(value, dict):
        raise GateError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def positive_int(value: object, label: str) -> int:
    """Require a positive signed-64-bit GitHub database identifier."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_GITHUB_ID:
        raise GateError(f"{label} must be a bounded positive integer")
    return value


def repository_name(value: object) -> str:
    """Require an ASCII GitHub owner/name pair."""
    if not isinstance(value, str) or not REPOSITORY_RE.fullmatch(value):
        raise GateError("repository must be an owner/name pair")
    if any(component in {".", ".."} for component in value.split("/")):
        raise GateError("repository contains an unsafe path segment")
    return value


def commit_sha(value: object, label: str) -> str:
    """Require a full lowercase Git commit SHA-1."""
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise GateError(f"{label} must be a full lowercase commit SHA")
    return value


def bounded_text(
    value: object,
    label: str,
    *,
    max_bytes: int,
    allow_empty: bool = False,
) -> str:
    """Require trimmed, control-free UTF-8 text within a byte limit."""
    if not isinstance(value, str) or (not value and not allow_empty):
        raise GateError(f"{label} must be a bounded string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise GateError(f"{label} must be valid UTF-8") from error
    if (
        len(encoded) > max_bytes
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GateError(f"{label} contains unsafe or oversized text")
    return value


def ref_name(value: object, label: str = "ref") -> str:
    """Require a bounded GitHub ref name without control characters."""
    return bounded_text(value, label, max_bytes=MAX_REF_BYTES)


def login(value: object, label: str = "login") -> str:
    """Require a bounded GitHub login."""
    return bounded_text(value, label, max_bytes=MAX_LOGIN_BYTES)


def timestamp(value: object, label: str) -> datetime:
    """Require a bounded timezone-aware ISO-8601 timestamp and normalize it to UTC."""
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        raise GateError(f"{label} must be a bounded UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GateError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GateError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def actor_is(value: object, *, user_id: int, user_login: str) -> bool:
    """Match both immutable database ID and login without accepting a lookalike."""
    if (
        not isinstance(value, dict)
        or isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or not 1 <= user_id <= MAX_GITHUB_ID
    ):
        return False
    user = value.get("user")
    if not isinstance(user, dict):
        return False
    observed_id = user.get("id")
    return (
        not isinstance(observed_id, bool)
        and isinstance(observed_id, int)
        and 1 <= observed_id <= MAX_GITHUB_ID
        and observed_id == user_id
        and user.get("login") == user_login
    )


def workflow_path(value: object, label: str) -> str:
    """Require a workflow file directly beneath .github/workflows."""
    if not isinstance(value, str) or not WORKFLOW_PATH_RE.fullmatch(value):
        raise GateError(f"{label} must be a repository workflow path")
    return value


def request_timeout(value: object) -> float:
    """Parse a bounded per-request timeout."""
    if isinstance(value, bool):
        raise GateError("request timeout must be a number")
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError) as error:
        raise GateError("request timeout must be a number") from error
    if not math.isfinite(parsed) or not 1.0 <= parsed <= MAX_REQUEST_TIMEOUT_SECONDS:
        raise GateError(
            f"request timeout must be between 1 and {MAX_REQUEST_TIMEOUT_SECONDS:g} seconds"
        )
    return parsed


def read_json_object(path: str, *, label: str, max_bytes: int = MAX_EVENT_BYTES) -> dict[str, Any]:
    """Read one bounded JSON object from disk."""
    if not path:
        raise GateError(f"{label} path is required")
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as error:
        raise GateError(f"{label} could not be read") from error
    if not raw or len(raw) > max_bytes:
        raise GateError(f"{label} is empty or too large")
    try:
        return record(json.loads(raw), label)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GateError(f"{label} is invalid JSON") from error


@dataclass(frozen=True)
class PullRequest:
    """Security-relevant fields from one live pull request."""

    number: int
    head_sha: str
    head_ref: str
    head_repository: str
    base_ref: str
    base_sha: str
    draft: bool
    state: str
    created_at: datetime
