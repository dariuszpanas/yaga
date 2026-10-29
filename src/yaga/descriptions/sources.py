"""Bounded explicit Markdown and GitHub event sources; no Git or network access."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yaga.descriptions.models import MAX_DESCRIPTION_BYTES
from yaga.errors import InputError, safe_error_text
from yaga.files import read_regular_file_prefix

MAX_EVENT_BYTES = 1024 * 1024
MAX_PULL_REQUEST_NUMBER = 2**63 - 1
SUPPORTED_ACTIONS = frozenset(
    {"opened", "reopened", "synchronize", "edited", "ready_for_review", "converted_to_draft"}
)


@dataclass(frozen=True, slots=True)
class DescriptionEvent:
    """Selected description fields, without an authentication claim."""

    number: int
    body: str


def read_description_file(path: Path) -> str:
    """Read an explicitly selected bounded regular UTF-8 Markdown file."""
    return _decode(_read(path, maximum=MAX_DESCRIPTION_BYTES), label="description file")


def read_description_stdin() -> str:
    """Read at most the description budget plus one byte from standard input."""
    binary_stream = getattr(sys.stdin, "buffer", None)
    try:
        if binary_stream is not None:
            raw = binary_stream.read(MAX_DESCRIPTION_BYTES + 1)
            if not isinstance(raw, bytes):
                raise InputError("standard input did not provide a byte stream")
            return _decode(raw, label="standard input")
        text = sys.stdin.read(MAX_DESCRIPTION_BYTES + 1)
        return _decode(text.encode("utf-8"), label="standard input")
    except (OSError, UnicodeError, ValueError) as error:
        raise InputError("standard input could not be read as UTF-8") from error


def load_description_event(path: Path) -> DescriptionEvent:
    """Parse one explicit event without requiring unrelated commit metadata."""
    raw = _read(path, maximum=MAX_EVENT_BYTES)
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError) as error:
        raise InputError("pull request event is invalid or ambiguous UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise InputError("pull request event must be an object")
    action = document.get("action")
    if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
        raise InputError("pull request event action is unsupported")
    pull_request = document.get("pull_request")
    if not isinstance(pull_request, dict):
        raise InputError("pull request event must contain a pull_request object")
    number = document.get("number")
    nested_number = pull_request.get("number")
    if (
        type(number) is not int
        or not 1 <= number <= MAX_PULL_REQUEST_NUMBER
        or type(nested_number) is not int
        or nested_number != number
    ):
        raise InputError("pull request numbers must be matching bounded positive integers")
    if pull_request.get("state") != "open":
        raise InputError("pull request state must be open")
    draft = pull_request.get("draft")
    if type(draft) is not bool:
        raise InputError("pull request draft state must be a boolean")
    if (action == "ready_for_review" and draft) or (action == "converted_to_draft" and not draft):
        raise InputError("pull request action and draft state are inconsistent")
    body = pull_request.get("body")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise InputError("pull request body must be a string or null")
    try:
        body = _decode(body.encode("utf-8"), label="pull request body")
    except UnicodeEncodeError as error:
        raise InputError("pull request body is not valid UTF-8") from error
    return DescriptionEvent(number=number, body=body)


def _read(path: Path, *, maximum: int) -> bytes:
    try:
        resolved = path.expanduser().resolve()
        raw = read_regular_file_prefix(resolved, maximum=maximum)
    except (OSError, RuntimeError, ValueError) as error:
        raise InputError(
            f"cannot read regular input file {safe_error_text(path, maximum=300)}"
        ) from error
    if len(raw) > maximum:
        raise InputError(f"input file exceeds the hard {maximum}-byte limit")
    return raw


def _decode(raw: bytes, *, label: str) -> str:
    if len(raw) > MAX_DESCRIPTION_BYTES:
        raise InputError(f"{label} exceeds the hard {MAX_DESCRIPTION_BYTES}-byte limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError(f"{label} is not valid UTF-8") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not (-float("inf") < number < float("inf")):
        raise ValueError("nonfinite JSON number")
    return number
