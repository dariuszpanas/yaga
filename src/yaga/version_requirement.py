"""Offline, dependency-free release requirements for installed and Action runtimes."""

from __future__ import annotations

import re

from yaga.errors import ConfigurationError

# Keep this equal to project.version; the build contract tests enforce that boundary.
VERSION = "0.1.2"
_RELEASE = r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
_CLAUSE = re.compile(r"(>=|<=|==|!=|>|<)\s*" + _RELEASE, re.ASCII)


def check_requirement(value: object, *, current: str = VERSION) -> str:
    """Require a bounded conjunction of comparisons against final X.Y.Z releases."""
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise ConfigurationError("required-version must be a string of 1 through 256 characters")
    clauses = value.split(",")
    if len(clauses) > 8:
        raise ConfigurationError("required-version permits at most eight comparisons")
    parsed: list[tuple[str, tuple[int, ...]]] = []
    for clause in clauses:
        match = _CLAUSE.fullmatch(clause.strip())
        if match is None:
            raise ConfigurationError(
                "required-version expects comma-separated >=, >, <=, <, == or != "
                "comparisons against final MAJOR.MINOR.PATCH releases"
            )
        parsed.append((match[1], tuple(int(part) for part in match.groups()[1:])))
    release = re.fullmatch(_RELEASE, current, re.ASCII)
    if release is None:
        raise ConfigurationError("cannot evaluate required-version for a non-final YAGA version")
    actual = tuple(int(part) for part in release.groups())
    for operator, expected in parsed:
        satisfied = {
            ">=": actual >= expected,
            ">": actual > expected,
            "<=": actual <= expected,
            "<": actual < expected,
            "==": actual == expected,
            "!=": actual != expected,
        }[operator]
        if not satisfied:
            raise ConfigurationError(
                f"YAGA {current} does not satisfy required-version {value!r}; "
                "update YAGA using its installation manager"
            )
    return value
