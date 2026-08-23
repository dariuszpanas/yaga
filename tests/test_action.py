"""Tests for YAGA's deliberately narrow public selector surface."""

from __future__ import annotations

import pytest

from yaga import cli
from yaga.action import SUPPORTED_GATES, gate_name
from yaga.errors import GateError


def test_only_the_implemented_gate_is_accepted() -> None:
    assert SUPPORTED_GATES == {"codex-review"}
    assert gate_name("codex-review") == "codex-review"
    for value in ("codex", "future-gate", "", None):
        with pytest.raises(GateError, match="gate is invalid"):
            gate_name(value)


def test_codex_selector_delegates_directly_to_the_single_shipped_runtime() -> None:
    assert cli._runtime("codex-review") is cli.run_action
