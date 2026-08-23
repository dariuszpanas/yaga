"""Strict public inputs for the YAGA composite action."""

from __future__ import annotations

from yaga.errors import GateError

SUPPORTED_GATES = frozenset({"codex-review"})


def gate_name(value: object) -> str:
    """Accept only a deliberately implemented gate."""
    if not isinstance(value, str) or value not in SUPPORTED_GATES:
        raise GateError("gate is invalid")
    return value
