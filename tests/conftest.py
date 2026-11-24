"""Shared pytest fixtures for packaging, installer, and Action shell checks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def native_action_binary() -> Path:
    """Return the built native ``yaga`` binary for Action shell control tests."""
    configured = os.environ.get("YAGA_TEST_NATIVE_BINARY")
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
    try:
        from yaga import find_yaga_bin

        return find_yaga_bin()
    except FileNotFoundError:
        pass
    name = "yaga.exe" if os.name == "nt" else "yaga"
    root = Path(__file__).resolve().parents[1]
    for profile in ("release", "debug"):
        candidate = root / "target" / profile / name
        if candidate.is_file():
            return candidate
    pytest.skip("built native yaga executable is unavailable")
