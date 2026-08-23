"""Tests for repository tooling that protects the distributed artifacts."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from scripts import run_actionlint

ROOT = Path(__file__).parents[1]


def test_actionlint_discovers_both_workflow_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_actionlint, "ROOT", tmp_path)
    workflows = tmp_path / ".github" / "workflows"
    examples = tmp_path / "examples"
    workflows.mkdir(parents=True)
    examples.mkdir()
    (workflows / "ci.yaml").write_text("name: CI\n", encoding="utf-8")
    (examples / "consumer.yml").write_text("name: Consumer\n", encoding="utf-8")

    assert run_actionlint.workflow_paths() == [
        ".github/workflows/ci.yaml",
        "examples/consumer.yml",
    ]


def test_toolchain_supply_chain_inputs_are_exactly_pinned() -> None:
    actionlint_image = run_actionlint.ACTIONLINT_IMAGE
    assert re.fullmatch(r"rhysd/actionlint@sha256:[0-9a-f]{64}", actionlint_image)
    assert ":latest" not in actionlint_image

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_requirements = project["build-system"]["requires"]
    assert build_requirements == ["hatchling==1.32.0"]
    assert "hatchling==1.32.0" in project["dependency-groups"]["dev"]
    assert project["tool"]["uv"]["required-version"] == "==0.9.18"

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert ci.count('version: "0.9.18"') == 2
