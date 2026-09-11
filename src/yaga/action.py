"""Strict public inputs for the YAGA composite action."""

from __future__ import annotations

from yaga.agent_review.runtime import operation_name, run_action
from yaga.errors import GateError

SUPPORTED_GATES = frozenset({"agent-review"})


def gate_name(value: object) -> str:
    """Accept only a deliberately implemented gate."""
    if not isinstance(value, str) or value not in SUPPORTED_GATES:
        raise GateError("gate is invalid")
    return value


def run_gate(gate: object, operation: object) -> int:
    """Run one closed gate operation through the shared dispatcher."""
    selected_gate = gate_name(gate)
    selected_operation = operation_name(operation)
    if selected_gate == "agent-review":
        return run_action(selected_operation)
    raise GateError("gate is invalid")  # pragma: no cover - gate_name is closed above
