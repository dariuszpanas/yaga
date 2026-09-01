"""Expected errors and dependency-free CLI boundary sanitization."""

from __future__ import annotations

import unicodedata


class YagaError(RuntimeError):
    """Base class for an expected YAGA operational failure."""

    kind = "runtime"


class GateError(YagaError):
    """Raised when evidence is missing, stale, malformed, or unsafe."""

    kind = "gate"


class ConfigurationError(YagaError):
    """Raised when a discovered policy file is invalid or unsafe."""

    kind = "configuration"


class GitError(YagaError):
    """Raised when Git cannot resolve a requested commit selection."""

    kind = "git"


class InputError(YagaError):
    """Raised when an explicit message source cannot be read."""

    kind = "input"


def safe_error_text(value: object, *, maximum: int = 1000) -> str:
    """Return one bounded line without terminal or bidirectional controls."""
    cleaned = "".join(
        "?" if unicodedata.category(character) in {"Cc", "Cf", "Cs"} else character
        for character in str(value)
    )
    collapsed = " ".join(cleaned.split())
    if len(collapsed) <= maximum:
        return collapsed
    return f"{collapsed[: maximum - 1]}…"
