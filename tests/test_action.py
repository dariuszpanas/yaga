"""Tests for YAGA's deliberately narrow Action selector surface."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from yaga import action, action_cli
from yaga.action import SUPPORTED_GATES, gate_name
from yaga.errors import GateError

ROOT = Path(__file__).parents[1]


def test_only_the_implemented_gate_is_accepted() -> None:
    assert SUPPORTED_GATES == {"codex-review"}
    assert gate_name("codex-review") == "codex-review"
    for value in ("codex", "future-gate", "", None):
        with pytest.raises(GateError, match="gate is invalid"):
            gate_name(value)


def test_codex_selector_delegates_to_the_single_shipped_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(action, "run_action", lambda operation: calls.append(operation) or 0)

    assert action.run_gate("codex-review", "prepare") == 0
    assert calls == ["prepare"]


def test_dependency_free_action_cli_uses_the_shared_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        action_cli,
        "run_gate",
        lambda gate, operation: calls.append((gate, operation)) or 0,
    )

    assert action_cli.main(["gate", "codex-review", "invalidate"]) == 0
    assert calls == [("codex-review", "invalidate")]


def test_dependency_free_action_cli_sanitizes_gate_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_gate: str, _operation: str) -> int:
        raise GateError("operation\u202e failed\x1b safely")

    monkeypatch.setattr(action_cli, "run_gate", fail)

    assert action_cli.main(["gate", "codex-review", "finalize"]) == 1
    assert capsys.readouterr().err == "YAGA failed: operation? failed? safely\n"


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--help"],
        ["gate", "codex-review", "--help", "extra"],
        ["other", "codex-review", "prepare"],
    ],
)
def test_dependency_free_action_cli_has_no_help_or_extra_argument_success_path(
    arguments: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert action_cli.main(arguments) == 1
    assert capsys.readouterr().err == "YAGA failed: action command is invalid\n"


def test_dependency_free_action_cli_rejects_help_as_an_operation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert action_cli.main(["gate", "codex-review", "--help"]) == 1
    assert capsys.readouterr().err == "YAGA failed: operation is invalid\n"


def test_composite_action_command_does_not_import_installed_dependencies() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["YAGA_ACTION_RUNTIME"] = "1"
    environment.pop("GITHUB_REPOSITORY", None)

    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-S",
            "-m",
            "yaga",
            "gate",
            "codex-review",
            "invalidate",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == (
        "YAGA failed: required GitHub environment is missing: GITHUB_REPOSITORY\n"
    )
    assert "typer" not in completed.stderr.casefold()
