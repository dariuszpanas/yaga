"""Bounded local JSON acquisition for explicit test-result evaluation inputs."""

from __future__ import annotations

import json
import math
from pathlib import Path

from yaga.errors import InputError
from yaga.files import read_regular_file_prefix
from yaga.test_planning.limits import MAX_PLAN_BYTES

MAX_INPUT_BYTES = MAX_PLAN_BYTES
MAX_JSON_DEPTH = 16
MAX_NUMBER_CHARACTERS = 64


def load_evaluation_json(path: Path) -> object:
    """Read one explicit regular UTF-8 JSON file without discovery or Git access.

    File acquisition owns the byte, duplicate-key, numeric and nesting boundaries;
    the pure document parsers own versioned fields and semantic validation.
    """
    if not isinstance(path, Path):
        raise InputError("test evaluation input path must be a Path")
    try:
        resolved = path.resolve()
        raw = read_regular_file_prefix(resolved, maximum=MAX_INPUT_BYTES)
    except (OSError, RuntimeError, ValueError) as error:
        raise InputError("cannot read a regular test evaluation input file") from error
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError(f"test evaluation input exceeds the hard {MAX_INPUT_BYTES}-byte limit")
    try:
        text = raw.decode("utf-8", errors="strict")
        _check_depth(text)
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_int=_integer,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise InputError("test evaluation input must be bounded UTF-8 JSON") from error


def _check_depth(text: str) -> None:
    """Bound containers before recursive JSON decoding, ignoring quoted tokens."""
    depth = 0
    quoted = escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("test evaluation JSON exceeds its depth limit")
        elif character in "]}":
            depth -= 1
    # The decoder validates delimiter balance and quoted-string syntax.


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("test evaluation JSON contains duplicate keys")
        result[key] = value
    return result


def _integer(value: str) -> int:
    if len(value) > MAX_NUMBER_CHARACTERS:
        raise ValueError("test evaluation JSON integer exceeds its digit limit")
    return int(value)


def _finite_float(value: str) -> float:
    if len(value) > MAX_NUMBER_CHARACTERS:
        raise ValueError("test evaluation JSON number exceeds its digit limit")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("test evaluation JSON requires finite numbers")
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError("test evaluation JSON requires finite numbers")
