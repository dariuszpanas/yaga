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
    commit_policy = project["tool"]["yaga"]["commit"]
    assert commit_policy["breaking-markers"] == "paired"
    assert commit_policy["scope-policy-by-type"] == {
        "feat": "required",
        "fix": "required",
    }
    assert commit_policy["forbidden-footer-tokens"] == ["WIP"]
    assert "required-footer-tokens" not in commit_policy

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "uv run pre-commit validate-manifest .pre-commit-hooks.yaml" in makefile
    assert "uv run yaga workflow lint .github/workflows examples" in makefile
    assert (
        "uv run yaga repo check --plan .yaga/checks/ci.toml --commit HEAD --revision HEAD"
        in makefile
    )
    assert (
        "uv run yaga change check --policy .yaga/change-policy.toml --range HEAD^..HEAD" in makefile
    )
    assert "uv run yaga mode check" not in makefile
    assert "uv run yaga path check" not in makefile
    assert "uv run yaga size check" not in makefile
    assert "uv run yaga tree check" not in makefile
    assert "uv run yaga repo check --check" not in makefile

    repository_plan = tomllib.loads(
        (ROOT / ".yaga" / "checks" / "ci.toml").read_text(encoding="utf-8")
    )
    assert repository_plan == {
        "plan-version": 2,
        "checks": [
            "commit",
            "workflow",
            "workflow-security",
            "workflow-lint",
            "mode",
            "path",
            "size",
            "tree",
        ],
        "workflow-paths": [".github/workflows", "examples"],
        "workflow-security-profile": "recommended-v3",
        "mode-policy": ".yaga/mode-policy.toml",
        "path-policy": ".yaga/path-policy.toml",
        "size-policy": ".yaga/size-policy.toml",
        "tree-policy": ".yaga/tree-policy.toml",
    }
    change_policy = tomllib.loads(
        (ROOT / ".yaga" / "change-policy.toml").read_text(encoding="utf-8")
    )
    assert change_policy == {
        "change-policy-version": 1,
        "rules": [
            {
                "name": "python-source-needs-tests",
                "when-any": ["src/**/*.py"],
                "require-any": ["tests/**/*.py"],
            }
        ],
    }
    branch_policy = tomllib.loads(
        (ROOT / ".yaga" / "branch-policy.toml").read_text(encoding="utf-8")
    )
    assert branch_policy == {
        "branch-policy-version": 1,
        "allowed-patterns": ["main", "feat/*", "fix/*", "dependabot/*/**"],
    }
    mode_policy = tomllib.loads((ROOT / ".yaga" / "mode-policy.toml").read_text(encoding="utf-8"))
    assert mode_policy == {
        "mode-policy-version": 1,
        "default-allowed-modes": ["regular"],
    }
    path_policy = tomllib.loads((ROOT / ".yaga" / "path-policy.toml").read_text(encoding="utf-8"))
    assert path_policy == {
        "path-policy-version": 1,
        "profile": "windows-compatible-v1",
    }
    size_policy = tomllib.loads((ROOT / ".yaga" / "size-policy.toml").read_text(encoding="utf-8"))
    assert size_policy == {
        "size-policy-version": 1,
        "default-max-blob-bytes": 131072,
        "max-total-blob-bytes": 8388608,
        "path-limits": [{"pattern": "uv.lock", "max-blob-bytes": 1048576}],
    }
    tree_policy = tomllib.loads((ROOT / ".yaga" / "tree-policy.toml").read_text(encoding="utf-8"))
    assert tree_policy == {
        "tree-policy-version": 1,
        "required-paths": [
            ".yaga/mode-policy.toml",
            ".yaga/path-policy.toml",
            ".yaga/size-policy.toml",
            ".yaga/tree-policy.toml",
            "LICENSE",
            "README.md",
            "SECURITY.md",
            "pyproject.toml",
            "uv.lock",
        ],
        "forbidden-patterns": [
            "**/.env",
            "**/__pycache__/**",
            "**/.mypy_cache/**",
            "**/.pytest_cache/**",
            "**/.ruff_cache/**",
            "**/.tox/**",
            "**/.venv/**",
            "**/*.pyc",
            "**/*.pyo",
            "build/**",
            "dist/**",
            "htmlcov/**",
            "**/.coverage",
        ],
    }

    for documentation in ("README.md", "CONTRIBUTING.md"):
        assert actionlint_image in (ROOT / documentation).read_text(encoding="utf-8")

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert ci.count('version: "0.9.18"') == 2
    assert "yaga --help" in ci
    assert "python -m yaga --help" in ci
    assert 'yaga commit check\n          --message "feat(ci): exercise the installed CLI"' in ci
    assert "Check pull-request branch name" in ci
    assert "Check pushed branch name" in ci
    assert ci.count("yaga branch check") == 2
    assert ci.count("--policy .yaga/branch-policy.toml") == 2
    assert "YAGA_BRANCH_NAME: ${{ github.head_ref }}" in ci
    assert "YAGA_BRANCH_NAME: ${{ github.ref_name }}" in ci
    assert ci.count('--name "$YAGA_BRANCH_NAME"') == 2
    for command in ("tree check", "path check", "mode check", "size check"):
        assert f"yaga {command}" not in ci
    assert "pre-commit try-repo . yaga-commit-check" in ci
    assert 'test "$YAGA_PRE_COMMIT_STATUS" -eq 1' in ci
    assert "grep -F -- '[syntax.header]'" in ci
    assert "Run aggregate repository checks" in ci
    assert "yaga repo check" in ci
    assert "--plan .yaga/checks/ci.toml" in ci
    assert (
        "YAGA_REPOSITORY_REVISION: ${{ github.event_name == 'pull_request' && "
        "github.event.pull_request.head.sha || github.sha }}" in ci
    )
    assert '--commit "$YAGA_REPOSITORY_REVISION"' in ci
    assert '--revision "$YAGA_REPOSITORY_REVISION"' in ci
    assert '--revision "${{ github.' not in ci
    assert "Run changed-path policy" in ci
    assert "yaga change check" in ci
    assert "--policy .yaga/change-policy.toml" in ci
    assert "fetch-depth: 0" in ci
    assert "YAGA_CHANGE_BASE" in ci
    assert "YAGA_CHANGE_HEAD" in ci
    assert "YAGA_CHANGE_SEPARATOR" in ci
    assert (
        "--check commit --check workflow --check workflow-security --check workflow-lint" not in ci
    )
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
    assert '"branch",\n            "check"' in build_gate
    assert build_gate.index("branch_completed = run_bounded") < build_gate.index(
        'git = shutil.which("git")'
    )
    assert '"workflow",\n            "check"' in build_gate
    assert '"workflow",\n            "security"' in build_gate
    assert '"workflow", "lint", "--help"' in build_gate
    assert '"repo",\n            "check"' in build_gate
    assert 'repository_plan_v2 = consumer / "repository-plan-v2.toml"' in build_gate
    assert 'operation="repository check plan v2"' in build_gate
    assert "repository plan v2 changed a standalone child report" in build_gate
    assert '"tree",\n            "check"' in build_gate
    assert '"mode",\n            "check"' in build_gate
    assert '"size",\n            "check"' in build_gate
    assert '"path",\n            "check"' in build_gate
    assert '"yaga/commands/repo.py"' in build_gate
    assert '"yaga/commands/change.py"' in build_gate
    assert '"yaga/commands/branch.py"' in build_gate
    assert '"yaga/commands/tree.py"' in build_gate
    assert '"yaga/commands/mode.py"' in build_gate
    assert '"yaga/commands/size.py"' in build_gate
    assert '"yaga/commands/path.py"' in build_gate
    assert '"yaga/git/__init__.py"' in build_gate
    assert '"yaga/git/process.py"' in build_gate
    assert '"yaga/git/runtime.py"' in build_gate
    assert '"yaga/git/tree.py"' in build_gate
    assert '"yaga/commits/service.py"' in build_gate
    assert '"yaga/changes/__init__.py"' in build_gate
    assert '"yaga/changes/checker.py"' in build_gate
    assert '"yaga/changes/git.py"' in build_gate
    assert '"yaga/changes/models.py"' in build_gate
    assert '"yaga/changes/patterns.py"' in build_gate
    assert '"yaga/changes/policy.py"' in build_gate
    assert '"yaga/changes/reporting.py"' in build_gate
    assert '"yaga/changes/service.py"' in build_gate
    assert '"yaga/branches/__init__.py"' in build_gate
    assert '"yaga/branches/checker.py"' in build_gate
    assert '"yaga/branches/models.py"' in build_gate
    assert '"yaga/branches/patterns.py"' in build_gate
    assert '"yaga/branches/policy.py"' in build_gate
    assert '"yaga/branches/reporting.py"' in build_gate
    assert '"yaga/branches/service.py"' in build_gate
    assert '"yaga/trees/__init__.py"' in build_gate
    assert '"yaga/trees/checker.py"' in build_gate
    assert '"yaga/trees/git.py"' in build_gate
    assert '"yaga/trees/models.py"' in build_gate
    assert '"yaga/trees/patterns.py"' in build_gate
    assert '"yaga/trees/policy.py"' in build_gate
    assert '"yaga/trees/reporting.py"' in build_gate
    assert '"yaga/trees/service.py"' in build_gate
    assert '"yaga/modes/__init__.py"' in build_gate
    assert '"yaga/modes/checker.py"' in build_gate
    assert '"yaga/modes/git.py"' in build_gate
    assert '"yaga/modes/models.py"' in build_gate
    assert '"yaga/modes/patterns.py"' in build_gate
    assert '"yaga/modes/policy.py"' in build_gate
    assert '"yaga/modes/reporting.py"' in build_gate
    assert '"yaga/modes/service.py"' in build_gate
    assert '"yaga/sizes/__init__.py"' in build_gate
    assert '"yaga/sizes/checker.py"' in build_gate
    assert '"yaga/sizes/git.py"' in build_gate
    assert '"yaga/sizes/models.py"' in build_gate
    assert '"yaga/sizes/patterns.py"' in build_gate
    assert '"yaga/sizes/policy.py"' in build_gate
    assert '"yaga/sizes/reporting.py"' in build_gate
    assert '"yaga/sizes/service.py"' in build_gate
    assert '"yaga/paths/__init__.py"' in build_gate
    assert '"yaga/paths/checker.py"' in build_gate
    assert '"yaga/paths/git.py"' in build_gate
    assert '"yaga/paths/models.py"' in build_gate
    assert '"yaga/paths/policy.py"' in build_gate
    assert '"yaga/paths/reporting.py"' in build_gate
    assert '"yaga/paths/service.py"' in build_gate
    assert '"yaga/repository/checker.py"' in build_gate
    assert '"yaga/repository/plan.py"' in build_gate
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
        assert "from yaga.changes" not in source, path
        assert "from yaga.branches" not in source, path
        assert "from yaga.trees" not in source, path
        assert "from yaga.sizes" not in source, path
        assert "from yaga.paths" not in source, path
        assert "from yaga.commands" not in source, path
        assert "from yaga.commits" not in source, path
        assert "from yaga.workflows" not in source, path
        assert "import yaga.changes" not in source, path
        assert "import yaga.branches" not in source, path
        assert "import yaga.trees" not in source, path
        assert "import yaga.sizes" not in source, path
        assert "import yaga.paths" not in source, path


def test_commit_action_import_graph_is_dependency_free_and_read_only() -> None:
    paths = [
        ROOT / "src" / "yaga" / "commit_action_cli.py",
        ROOT / "src" / "yaga" / "commit_action_runtime.py",
        ROOT / "src" / "yaga" / "files.py",
        *(ROOT / "src" / "yaga" / "git").glob("*.py"),
        *(ROOT / "src" / "yaga" / "commits").glob("*.py"),
    ]

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "import typer" not in source, path
        assert "from yaga.cli" not in source, path
        assert "from yaga.changes" not in source, path
        assert "from yaga.branches" not in source, path
        assert "from yaga.trees" not in source, path
        assert "from yaga.sizes" not in source, path
        assert "from yaga.paths" not in source, path
        assert "from yaga.commands" not in source, path
        assert "from yaga.codex" not in source, path
        assert "from yaga.workflows" not in source, path
        assert "from yaga.github import" not in source, path
        assert "import yaga.changes" not in source, path
        assert "import yaga.branches" not in source, path
        assert "import yaga.trees" not in source, path
        assert "import yaga.sizes" not in source, path
        assert "import yaga.paths" not in source, path
        assert "GITHUB_TOKEN" not in source, path
