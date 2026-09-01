"""Tests for repository tooling that protects the distributed artifacts."""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import check_build
from yaga.workflows.lint import ACTIONLINT_IMAGE

ROOT = Path(__file__).parents[1]


def test_toolchain_supply_chain_inputs_are_exactly_pinned() -> None:
    actionlint_image = ACTIONLINT_IMAGE
    assert actionlint_image == (
        "rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
    )
    assert re.fullmatch(r"rhysd/actionlint@sha256:[0-9a-f]{64}", actionlint_image)
    assert ":latest" not in actionlint_image

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "yaga-cli"
    assert project["project"]["scripts"] == {"yaga": "yaga.cli:main"}
    assert project["project"]["dependencies"] == [
        "pyyaml>=6.0.3,<7",
        "typer>=0.27.2,<1",
    ]
    build_requirements = project["build-system"]["requires"]
    assert build_requirements == ["hatchling==1.32.0"]
    assert "hatchling==1.32.0" in project["dependency-groups"]["dev"]
    assert "pre-commit>=4.6.2,<5" in project["dependency-groups"]["dev"]
    assert project["tool"]["uv"]["required-version"] == "==0.9.18"

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "uv run pre-commit validate-manifest .pre-commit-hooks.yaml" in makefile
    assert "uv run yaga workflow lint .github/workflows examples" in makefile
    assert (
        "uv run yaga repo check --check commit --check workflow --check workflow-security "
        "--check workflow-lint "
        "--commit HEAD --workflow-path .github/workflows --workflow-path examples"
    ) in makefile

    for documentation in ("README.md", "CONTRIBUTING.md"):
        assert actionlint_image in (ROOT / documentation).read_text(encoding="utf-8")

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert ci.count('version: "0.9.18"') == 2
    assert "yaga --help" in ci
    assert "python -m yaga --help" in ci
    assert 'yaga commit check\n          --message "feat(ci): exercise the installed CLI"' in ci
    assert "pre-commit try-repo . yaga-commit-check" in ci
    assert 'test "$YAGA_PRE_COMMIT_STATUS" -eq 1' in ci
    assert "grep -F -- '[syntax.header]'" in ci
    assert "Run aggregate repository checks" in ci
    assert "yaga repo check" in ci
    assert "--check commit --check workflow --check workflow-security --check workflow-lint" in ci
    assert (
        "ref: ${{ github.event_name == 'pull_request' && "
        "github.event.pull_request.head.sha || github.sha }}"
    ) in ci
    assert "Exercise the cached actionlint integration" in ci
    assert "tests/test_workflow_lint_hardening.py" in ci
    assert "scripts/run_actionlint.py" not in ci
    assert not (ROOT / "scripts" / "run_actionlint.py").exists()

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
    assert 'EXPECTED_REQUIRES_DIST = ["pyyaml<7,>=6.0.3", "typer<1,>=0.27.2"]' in build_gate
    assert '"PYTHONPATH"' in build_gate
    assert '"YAGA_ACTION_RUNTIME"' in build_gate
    assert "cwd=consumer" in build_gate
    assert '"config",\n            "init"' in build_gate
    assert '"workflow",\n            "check"' in build_gate
    assert '"workflow",\n            "security"' in build_gate
    assert '"workflow", "lint", "--help"' in build_gate
    assert '"repo",\n            "check"' in build_gate
    assert '"yaga/commands/repo.py"' in build_gate
    assert '"yaga/commits/service.py"' in build_gate
    assert '"yaga/repository/checker.py"' in build_gate
    assert '"yaga/workflows/inputs.py"' in build_gate
    assert '"yaga/workflows/lint.py"' in build_gate
    assert '"yaga/workflows/parser.py"' in build_gate
    assert '"yaga/workflows/security.py"' in build_gate
    assert '"yaga/workflows/security_facts.py"' in build_gate
    assert '"yaga/workflows/security_models.py"' in build_gate
    assert '"yaga/workflows/security_reporting.py"' in build_gate
    assert '"yaga/workflows/security_rules.py"' in build_gate
    assert "config.write_text" not in build_gate
    assert 'completed.stdout.decode("utf-8")' in build_gate
    assert '"pip",\n            "check"' in build_gate
    assert '"--format",\n            "json"' in build_gate


def test_wheel_metadata_must_advertise_the_locked_runtime() -> None:
    valid = (
        b"Metadata-Version: 2.4\n"
        b"Requires-Python: >=3.12\n"
        b"Requires-Dist: pyyaml<7,>=6.0.3\n"
        b"Requires-Dist: typer<1,>=0.27.2\n\n"
    )
    check_build.validate_wheel_metadata(valid)

    for malformed in (
        valid.replace(b"Requires-Dist: pyyaml<7,>=6.0.3\n", b""),
        valid.replace(b"Requires-Dist: typer<1,>=0.27.2\n", b""),
        valid.replace(b"pyyaml<7,>=6.0.3", b"pyyaml>=6.0.3"),
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
        assert "from yaga.workflows" not in source, path


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
        assert "from yaga.workflows" not in source, path
        assert "from yaga.github import" not in source, path
        assert "GITHUB_TOKEN" not in source, path
