"""Strict public inputs for the YAGA composite action."""

from __future__ import annotations

from yaga.errors import GateError

SUPPORTED_GATES = frozenset({"codex-review"})
ACTION_MODES = frozenset(
    {
        "invalidate-boundary",
        "repair-boundaries",
        "reconcile-boundary",
        "reconcile-candidate",
        "reconcile-repair-candidate",
        "resolve",
    }
)


def gate_name(value: object) -> str:
    """Accept only a deliberately implemented gate."""
    if not isinstance(value, str) or value not in SUPPORTED_GATES:
        raise GateError("gate is invalid")
    return value


def action_mode(value: object) -> str:
    """Require one internal composite-action operation."""
    if not isinstance(value, str) or value not in ACTION_MODES:
        raise GateError("action mode is invalid")
    return value
