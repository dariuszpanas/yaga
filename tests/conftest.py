"""Shared pytest isolation guards."""

from __future__ import annotations

import pytest

_RUNNER_COMMAND_ENV_VARS = (
    "GITHUB_ENV",
    "GITHUB_OUTPUT",
    "GITHUB_PATH",
    "GITHUB_STATE",
    "GITHUB_STEP_SUMMARY",
)


@pytest.fixture(autouse=True)
def _isolate_github_runner_command_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from writing to live GitHub Actions runner command files."""
    for name in _RUNNER_COMMAND_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
