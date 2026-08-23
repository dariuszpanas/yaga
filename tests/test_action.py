"""Tests for YAGA's deliberately narrow public selector surface."""

from __future__ import annotations

import pytest

from yaga import cli
from yaga.action import ACTION_MODES, SUPPORTED_GATES, action_mode, gate_name
from yaga.errors import GateError


def test_only_the_implemented_gate_is_accepted() -> None:
    assert SUPPORTED_GATES == {"codex-review"}
    assert gate_name("codex-review") == "codex-review"
    for value in ("codex", "future-gate", "", None):
        with pytest.raises(GateError, match="gate is invalid"):
            gate_name(value)


def test_action_modes_are_closed_and_explicit() -> None:
    assert ACTION_MODES == {
        "invalidate-boundary",
        "repair-boundaries",
        "reconcile-boundary",
        "reconcile-candidate",
        "reconcile-repair-candidate",
        "resolve",
    }
    for mode in ACTION_MODES:
        assert action_mode(mode) == mode
    for value in ("publish", "restore", "", None):
        with pytest.raises(GateError, match="action mode is invalid"):
            action_mode(value)


def test_codex_selector_delegates_directly_to_the_shipped_runtime() -> None:
    assert cli._runtime("codex-review") is cli.run_action
