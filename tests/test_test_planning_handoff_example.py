"""Runnable fixed-adapter examples preserve all catalog cells without execution."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from yaga.test_planning.handoff import create_handoff
from yaga.test_planning.planner import plan_tests
from yaga.test_planning.policy import load_test_policy
from yaga.test_planning.reports import PlannedTests
from yaga.test_planning.source import PlanSource

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "examples/test-planning/fixed_handoff.py"
POLICIES = ROOT / "docs/designs/examples/test-planning"


@pytest.mark.parametrize(
    ("changed_path", "required_dispatch"),
    [
        ("backend/src/worker.py", "backend-unit"),
        ("frontend/src/widget.ts", "frontend-unit"),
        ("tests/container/smoke.py", "container"),
        ("tests/chainsaw/check.yaml", "chainsaw"),
        ("qualification/ray/runner.py", "ray"),
    ],
)
def test_polyglot_example_hands_off_each_execution_category(
    changed_path: str, required_dispatch: str
) -> None:
    namespace = runpy.run_path(str(ADAPTER))
    registry = namespace["example_registry"]("polyglot")
    loaded = load_test_policy(POLICIES / "polyglot.toml")
    selection = plan_tests(loaded.policy, profile="development", changed_paths=(changed_path,))
    source = PlanSource("a" * 40, "b" * 40, "a" * 40, "b" * 40, "c" * 40)
    handoff = create_handoff(
        PlannedTests(loaded.sha256, registry.sha256, source, selection),
        registry,
        expected_policy_sha256=loaded.sha256,
        expected_execution_commit="b" * 40,
        expected_execution_tree="c" * 40,
    )
    assert handoff["dispatch"][required_dispatch] is True
    assert handoff["dispatch"]["repository"] is True
    assert len(handoff["dispatch"]) == 10
    assert "outcome" not in handoff


@pytest.mark.parametrize(("catalog", "jobs", "cells"), [("polyglot", 10, 11), ("django-ray", 5, 8)])
def test_example_runs_with_explicit_source_and_rejects_wrong_tree(
    tmp_path: Path, catalog: str, jobs: int, cells: int
) -> None:
    def git(*arguments: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *arguments], text=True, stderr=subprocess.PIPE
        ).strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "YAGA Test")
    git("config", "user.email", "test@example.invalid")
    git("commit", "--allow-empty", "-m", "test: create source")
    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    command = [
        sys.executable,
        str(ADAPTER),
        "--catalog",
        catalog,
        "--policy",
        str(POLICIES / f"{catalog}.toml"),
        "--profile",
        "integration",
        "--revision",
        commit,
        "--execution-commit",
        commit,
        "--execution-tree",
        tree,
    ]
    completed = subprocess.run(command, cwd=tmp_path, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert not completed.stderr
    handoff = json.loads(completed.stdout)
    assert handoff["test-handoff-version"] == 1
    assert len(handoff["dispatch"]) == jobs
    assert all(handoff["dispatch"].values())
    assert len(handoff["selected"]) == jobs
    assert len(handoff["cells"]) == cells
    assert handoff["source"]["execution_commit"] == commit
    assert handoff["source"]["execution_tree"] == tree
    if catalog == "django-ray":
        assert sum(item["dispatch"] == "native" for item in handoff["selected"]) == 1
        assert sum(item["dispatch"] == "native" for item in handoff["cells"]) == 4

    rejected = subprocess.run(
        [*command[:-1], "f" * len(tree)], cwd=tmp_path, capture_output=True, check=False
    )
    assert rejected.returncode == 2
    assert not rejected.stdout
    assert b"test handoff failed (input)" in rejected.stderr
