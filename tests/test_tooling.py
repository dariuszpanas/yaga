"""Tests for repository tooling that protects the distributed artifacts."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import check_build, run_actionlint

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


def test_actionlint_streams_bounded_workflows_without_a_host_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_actionlint, "ROOT", tmp_path)
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_bytes(b"name: CI\n")
    calls: list[tuple[list[str], bytes]] = []

    def fake_run(command: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        assert options["check"] is False
        workflow_input = options["input"]
        assert isinstance(workflow_input, bytes)
        calls.append((command, workflow_input))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(run_actionlint.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(run_actionlint.subprocess, "run", fake_run)

    assert run_actionlint.main() == 0
    assert calls == [
        (
            [
                "docker",
                "run",
                "--rm",
                "-i",
                run_actionlint.ACTIONLINT_IMAGE,
                "-color",
                "-stdin-filename",
                ".github/workflows/ci.yml",
                "-",
            ],
            b"name: CI\n",
        )
    ]
    assert "--volume" not in calls[0][0]


def test_toolchain_supply_chain_inputs_are_exactly_pinned() -> None:
    actionlint_image = run_actionlint.ACTIONLINT_IMAGE
    assert re.fullmatch(r"rhysd/actionlint@sha256:[0-9a-f]{64}", actionlint_image)
    assert ":latest" not in actionlint_image

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "yaga-cli"
    assert project["project"]["scripts"] == {"yaga": "yaga.cli:main"}
    assert project["project"]["dependencies"] == ["typer>=0.27.2,<1"]
    build_requirements = project["build-system"]["requires"]
    assert build_requirements == ["hatchling==1.32.0"]
    assert "hatchling==1.32.0" in project["dependency-groups"]["dev"]
    assert "pre-commit>=4.6.2,<5" in project["dependency-groups"]["dev"]
    assert project["tool"]["uv"]["required-version"] == "==0.9.18"

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "uv run pre-commit validate-manifest .pre-commit-hooks.yaml" in makefile

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert ci.count('version: "0.9.18"') == 2
    assert "yaga --help" in ci
    assert "python -m yaga --help" in ci
    assert 'yaga commit check\n          --message "feat(ci): exercise the installed CLI"' in ci
    assert "pre-commit try-repo . yaga-commit-check" in ci
    assert 'test "$YAGA_PRE_COMMIT_STATUS" -eq 1' in ci
    assert "grep -F -- '[syntax.header]'" in ci

    build_gate = (ROOT / "scripts" / "check_build.py").read_text(encoding="utf-8")
    for locked_argument in (
        "--locked",
        "--no-sources",
        "--no-default-groups",
        "--require-hashes",
        "--no-build",
        "--no-deps",
    ):
        assert f'"{locked_argument}"' in build_gate
    assert '"--no-hashes"' not in build_gate
    assert 'EXPECTED_REQUIRES_DIST = ["typer<1,>=0.27.2"]' in build_gate
    assert '"PYTHONPATH"' in build_gate
    assert '"YAGA_ACTION_RUNTIME"' in build_gate
    assert "cwd=consumer" in build_gate
    assert 'completed.stdout.decode("utf-8")' in build_gate
    assert '"pip",\n            "check"' in build_gate
    assert '"--format",\n            "json"' in build_gate


def test_wheel_metadata_must_advertise_the_locked_runtime() -> None:
    valid = b"Metadata-Version: 2.4\nRequires-Python: >=3.12\nRequires-Dist: typer<1,>=0.27.2\n\n"
    check_build.validate_wheel_metadata(valid)

    for malformed in (
        b"Metadata-Version: 2.4\nRequires-Python: >=3.12\n\n",
        valid.replace(b"typer<1,>=0.27.2", b"typer>=0.27.2"),
        valid.replace(
            b"Requires-Dist: typer<1,>=0.27.2",
            b"Requires-Dist: typer<1,>=0.27.2\nRequires-Dist: requests>=2",
        ),
        valid.replace(b"Requires-Python: >=3.12", b"Requires-Python: >=3.11"),
    ):
        with pytest.raises(SystemExit):
            check_build.validate_wheel_metadata(malformed)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_wheel_smoke_process_output_is_bounded(tmp_path: Path, stream: str) -> None:
    command = [
        sys.executable,
        "-c",
        (f"import sys; sys.{stream}.buffer.write(b'x' * {check_build.MAX_SMOKE_OUTPUT_BYTES + 1})"),
    ]

    with pytest.raises(SystemExit, match="output exceeds"):
        check_build.run_bounded(command, cwd=tmp_path, env=os.environ.copy())


def test_composite_action_import_graph_does_not_depend_on_installed_cli() -> None:
    paths = [
        ROOT / "src" / "yaga" / "action.py",
        ROOT / "src" / "yaga" / "action_cli.py",
        ROOT / "src" / "yaga" / "github.py",
        ROOT / "src" / "yaga" / "models.py",
        ROOT / "src" / "yaga" / "status.py",
        *(ROOT / "src" / "yaga" / "codex").glob("*.py"),
    ]

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "import typer" not in source, path
        assert "from yaga.cli" not in source, path
        assert "from yaga.commands" not in source, path
        assert "from yaga.commits" not in source, path


def test_commit_action_import_graph_is_dependency_free_and_read_only() -> None:
    paths = [
        ROOT / "src" / "yaga" / "commit_action_cli.py",
        ROOT / "src" / "yaga" / "commit_action_runtime.py",
        ROOT / "src" / "yaga" / "files.py",
        *(ROOT / "src" / "yaga" / "commits").glob("*.py"),
    ]

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "import typer" not in source, path
        assert "from yaga.cli" not in source, path
        assert "from yaga.commands" not in source, path
        assert "from yaga.codex" not in source, path
        assert "from yaga.github import" not in source, path
        assert "GITHUB_TOKEN" not in source, path
