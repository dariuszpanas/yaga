"""Tests for repository tooling that protects the distributed artifacts."""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from scripts import check_build, check_dependency_audit
from yaga.version_requirement import VERSION
from yaga.workflows.lint import ACTIONLINT_IMAGE

ROOT = Path(__file__).parents[1]


def _extract_exact_pin(requirements: list[str], package: str) -> str:
    matches: list[str] = []
    for item in requirements:
        requirement = Requirement(item)
        if canonicalize_name(requirement.name) != package:
            continue
        if (
            requirement.extras
            or requirement.marker is not None
            or requirement.url is not None
            or not item.startswith(f"{package}==")
        ):
            raise ValueError(f"{package} requirement must be an exact pin")
        version = item.removeprefix(f"{package}==")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version) or str(Version(version)) != version:
            raise ValueError(f"{package} version is not canonical")
        matches.append(version)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {package} pin")
    return matches[0]


def validate_exact_supply_chain_pins(
    project: dict[str, object], lock: dict[str, object]
) -> tuple[str, str]:
    build_system = project.get("build-system")
    assert isinstance(build_system, dict)
    build_requirements = build_system.get("requires")
    if not isinstance(build_requirements, list) or len(build_requirements) != 1:
        raise ValueError("build-system.requires must contain exactly one requirement")
    hatchling_version = _extract_exact_pin(build_requirements, "hatchling")

    dependency_groups = project.get("dependency-groups")
    assert isinstance(dependency_groups, dict)
    dev_requirements = dependency_groups.get("dev")
    if not isinstance(dev_requirements, list):
        raise ValueError("dependency-groups.dev must be a list")
    if _extract_exact_pin(dev_requirements, "hatchling") != hatchling_version:
        raise ValueError("hatchling build and dev pins do not match")
    pip_audit_version = _extract_exact_pin(dev_requirements, "pip-audit")

    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ValueError("uv.lock package table is missing")
    for package_name, expected_version in (
        ("hatchling", hatchling_version),
        ("pip-audit", pip_audit_version),
    ):
        entries = [
            entry
            for entry in packages
            if isinstance(entry, dict) and entry.get("name") == package_name
        ]
        if len(entries) != 1:
            raise ValueError(f"uv.lock must contain one {package_name} package entry")
        entry = entries[0]
        if entry.get("version") != expected_version:
            raise ValueError(f"uv.lock {package_name} version does not match pyproject.toml")
        if entry.get("source") != {"registry": "https://pypi.org/simple"}:
            raise ValueError(f"uv.lock {package_name} source must be PyPI")
        sdist = entry.get("sdist")
        if not isinstance(sdist, dict) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(sdist.get("hash", ""))
        ):
            raise ValueError(f"uv.lock {package_name} sdist hash is missing")
        wheels = entry.get("wheels")
        if (
            not isinstance(wheels, list)
            or not wheels
            or not all(
                isinstance(wheel, dict)
                and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", str(wheel.get("hash", ""))))
                for wheel in wheels
            )
        ):
            raise ValueError(f"uv.lock {package_name} wheel hashes are missing")

    yaga_entries = [
        entry for entry in packages if isinstance(entry, dict) and entry.get("name") == "yaga-cli"
    ]
    if len(yaga_entries) != 1:
        raise ValueError("uv.lock must contain one yaga-cli package entry")
    metadata = yaga_entries[0].get("metadata")
    requires_dev = (
        metadata.get("requires-dev", {}).get("dev") if isinstance(metadata, dict) else None
    )
    if not isinstance(requires_dev, list):
        raise ValueError("uv.lock yaga-cli requires-dev metadata is missing")
    for package_name, expected_version in (
        ("hatchling", hatchling_version),
        ("pip-audit", pip_audit_version),
    ):
        specifiers = [
            item
            for item in requires_dev
            if isinstance(item, dict) and item.get("name") == package_name
        ]
        if specifiers != [{"name": package_name, "specifier": f"=={expected_version}"}]:
            raise ValueError(f"uv.lock requires-dev entry for {package_name} does not match")
    return hatchling_version, pip_audit_version


def test_installed_description_smoke_preserves_the_explicit_template_path(tmp_path: Path) -> None:
    intermediate = tmp_path / "intermediate"
    intermediate.mkdir()
    consumer = intermediate / ".." / "consumer"
    consumer.mkdir()
    assert consumer.parent != consumer.parent.resolve()
    executable = Path(sys.executable).with_name("yaga.exe" if os.name == "nt" else "yaga")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PYTHON")
        and key not in {"YAGA_ACTION_RUNTIME", "YAGA_COMMIT_ACTION_RUNTIME"}
    }
    check_build.exercise_installed_description(executable, consumer=consumer, env=environment)


def test_toolchain_supply_chain_inputs_are_exactly_pinned() -> None:
    actionlint_image = ACTIONLINT_IMAGE
    assert actionlint_image == (
        "rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
    )
    assert re.fullmatch(r"rhysd/actionlint@sha256:[0-9a-f]{64}", actionlint_image)
    assert ":latest" not in actionlint_image

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "yaga-cli"
    assert project["project"]["version"] == VERSION
    assert project["project"]["scripts"] == {"yaga": "yaga.cli:main"}
    assert project["project"]["dependencies"] == [
        "pydantic>=2.14.2,<3",
        "pydantic-versions>=1.0.4,<2",
        "pygments>=2.20.0,<3",
        "pyyaml>=6.0.3,<7",
        "typer>=0.27.2,<1",
    ]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    _, pip_audit_version = validate_exact_supply_chain_pins(project, lock)
    assert pip_audit_version == check_dependency_audit.AUDITOR_VERSION

    dependabot = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert dependabot == {
        "version": 2,
        "updates": [
            {
                "package-ecosystem": "github-actions",
                "directory": "/",
                "schedule": {"interval": "weekly"},
            },
            {
                "package-ecosystem": "uv",
                "directory": "/",
                "schedule": {"interval": "weekly"},
                "versioning-strategy": "increase-if-necessary",
            },
        ],
    }
    assert "pre-commit>=4.6.2,<5" in project["dependency-groups"]["dev"]
    assert "pytest-xdist>=3.6" in project["dependency-groups"]["dev"]
    assert (
        project["tool"]["pytest"]["ini_options"]["addopts"]
        == "-v --tb=short -n auto --dist worksteal"
    )
    assert project["tool"]["uv"]["required-version"] == ">=0.12.7"
    commit_policy = project["tool"]["yaga"]["commit"]
    assert commit_policy["breaking-markers"] == "paired"
    assert commit_policy["scope-policy-by-type"] == {
        "feat": "required",
        "fix": "required",
    }
    assert commit_policy["forbidden-footer-tokens"] == ["WIP"]
    assert "required-footer-tokens" not in commit_policy

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "DOCS_ADDR ?= localhost:9000" in makefile
    assert 'uv run zensical serve --dev-addr "$(DOCS_ADDR)"' in makefile
    assert "uv run pre-commit validate-manifest .pre-commit-hooks.yaml" in makefile
    assert "uv run yaga policy check --strict" in makefile
    assert "uv run yaga workflow lint .github/workflows examples" in makefile
    assert (
        "uv run yaga repo check --plan .yaga/checks/ci.toml --commit HEAD --revision HEAD"
        in makefile
    )
    assert "CHANGE_RANGE ?= HEAD^..HEAD" in makefile
    assert (
        'uv run yaga change check --policy .yaga/change-policy.toml --range "$(CHANGE_RANGE)"'
        in makefile
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
        "path-limits": [
            {"pattern": "uv.lock", "max-blob-bytes": 1048576},
            {"pattern": "docs/assets/branding/*.png", "max-blob-bytes": 2097152},
        ],
    }
    description_policy = tomllib.loads(
        (ROOT / ".yaga" / "description-policy.toml").read_text(encoding="utf-8")
    )
    assert description_policy == {
        "description-policy-version": 1,
        "min-words": 8,
        "max-length": 65536,
        "placeholders": ["TODO", "TBD", "FIXME"],
        "require-checked-tasks": True,
        "typos": "check",
        "heading-min-level": 2,
        "heading-max-length": 120,
    }
    tree_policy = tomllib.loads((ROOT / ".yaga" / "tree-policy.toml").read_text(encoding="utf-8"))
    assert tree_policy == {
        "tree-policy-version": 1,
        "required-paths": [
            ".yaga/agent-review-policy.toml",
            ".yaga/branch-policy.toml",
            ".yaga/change-policy.toml",
            ".yaga/checks/ci.toml",
            ".yaga/description-policy.toml",
            ".yaga/mode-policy.toml",
            ".yaga/path-policy.toml",
            ".yaga/size-policy.toml",
            ".yaga/tree-policy.toml",
            "LICENSE",
            "README.md",
            "SECURITY.md",
            "action.yml",
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

    for documentation in ("docs/workflow-checks.md", "CONTRIBUTING.md"):
        assert actionlint_image in (ROOT / documentation).read_text(encoding="utf-8")

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    commit_policy_wf = (ROOT / ".github" / "workflows" / "commit-policy.yml").read_text(
        encoding="utf-8"
    )
    assert "name: YAGA Checks (branch, change, policy, repo)" in ci
    assert "name: Lint, Package Build & Docs" in ci
    assert ci.count('version: "0.13.0"') == 3
    assert "Audit YAGA policy schemas" in ci
    assert "check: policy" in ci
    for workflow_name, expected_uv_count in (
        ("release.yml", 2),
        ("docs.yml", 1),
        ("commit-quality.yml", 1),
    ):
        workflow_source = (ROOT / ".github" / "workflows" / workflow_name).read_text(
            encoding="utf-8"
        )
        assert workflow_source.count('version: "0.13.0"') == expected_uv_count
        assert 'version: "0.12.7"' not in workflow_source
    assert "yaga pull-request review check" not in ci
    assert "yaga --help" in ci
    assert "python -m yaga --help" in ci
    assert "yaga_message=\"$(printf '%s\\n\\n%s'" in ci
    assert "feat(ci): exercise the installed CLI" in ci
    assert "Check pull-request branch name" in ci
    assert "Check pushed branch name" in ci
    assert ci.count("check: branch") == 2
    assert ci.count("policy: .yaga/branch-policy.toml") == 2
    assert "branch: ${{ github.head_ref }}" in ci
    assert "branch: ${{ github.ref_name }}" in ci
    assert "Check pull-request description" in commit_policy_wf
    assert "check: pr-description" in commit_policy_wf
    assert "policy: .yaga/description-policy.toml" in commit_policy_wf
    for command in ("tree check", "path check", "mode check", "size check"):
        assert f"yaga {command}" not in ci
    assert "pre-commit try-repo . yaga-commit-check" in ci
    assert 'test "$YAGA_PRE_COMMIT_STATUS" -eq 1' in ci
    assert "grep -F -- '[syntax.header]'" in ci
    assert "Run aggregate repository checks" in ci
    assert "check: repo" in ci
    assert "plan: .yaga/checks/ci.toml" in ci
    assert (
        "commit: ${{ github.event_name == 'pull_request' && "
        "github.event.pull_request.head.sha || github.sha }}" in ci
    )
    assert (
        "revision: ${{ github.event_name == 'pull_request' && "
        "github.event.pull_request.head.sha || github.sha }}" in ci
    )
    assert "Run changed-path policy" in ci
    assert "check: change" in ci
    assert "policy: .yaga/change-policy.toml" in ci
    assert "fetch-depth: 0" in ci
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
        "--no-default-groups",
        "--require-hashes",
        "--no-build",
        "--no-deps",
    ):
        assert f'"{locked_argument}"' in build_gate
    assert '"--no-hashes"' not in build_gate
    assert check_build.EXPECTED_REQUIRES_DIST == [
        "pydantic-versions<2,>=1.0.4",
        "pydantic<3,>=2.14.2",
        "pygments<3,>=2.20.0",
        "pyyaml<7,>=6.0.3",
        "typer<1,>=0.27.2",
    ]
    assert "validate_sdist_metadata(source_distribution)" in build_gate
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
    assert '"yaga/cli_action.py"' in build_gate
    assert '"yaga/completion.py"' in build_gate
    assert '"yaga/commands/policy.py"' in build_gate
    assert '"yaga/policies/schemas.py"' in build_gate
    assert '"yaga/policies/audit.py"' in build_gate
    assert '"yaga/policies/init_templates.py"' in build_gate
    assert '"yaga/policies/selector.py"' in build_gate
    assert '"yaga/commands/repo.py"' in build_gate
    assert '"yaga/commands/change.py"' in build_gate
    assert '"yaga/commands/branch.py"' in build_gate
    assert '"yaga/commands/tree.py"' in build_gate
    assert '"yaga/commands/mode.py"' in build_gate
    assert '"yaga/commands/size.py"' in build_gate
    assert '"yaga/commands/path.py"' in build_gate
    assert '"yaga/commands/test.py"' in build_gate
    assert '"yaga/git/__init__.py"' in build_gate
    assert '"yaga/git/changes.py"' in build_gate
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
    for module in (
        "__init__",
        "git",
        "handoff",
        "limits",
        "models",
        "planner",
        "policy",
        "reporting",
        "reports",
        "selection",
        "service",
        "source",
    ):
        assert f'"yaga/test_planning/{module}.py"' in build_gate
    assert 'operation="test policy check"' in build_gate
    assert 'operation="policy init"' in build_gate
    assert 'operation="policy check"' in build_gate
    assert 'operation=f"test plan {profile}"' in build_gate
    assert "create_handoff(planned, registry, **expected)" in build_gate
    assert '"rejected_handoffs": [True, True, True]' in build_gate
    assert build_gate.index("test_policy = exercise_installed_test_policy(") < build_gate.index(
        '("init", "--initial-branch=main", "--template=")'
    )
    assert "tree_sha=tree_sha" in build_gate
    assert "invalid.returncode != 2 or invalid.stdout" in build_gate
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


@pytest.fixture
def runtime_metadata() -> bytes:
    return (
        b"Metadata-Version: 2.4\n"
        b"Requires-Python: >=3.12\n"
        b"Description-Content-Type: text/markdown\n"
        b"Requires-Dist: pydantic-versions<2,>=1.0.4\n"
        b"Requires-Dist: pydantic<3,>=2.14.2\n"
        b"Requires-Dist: pygments<3,>=2.20.0\n"
        b"Requires-Dist: pyyaml<7,>=6.0.3\n"
        b"Requires-Dist: typer<1,>=0.27.2\n"
        b"Requires-Dist: boto3<2,>=1.37; extra == 'quality-bedrock'\n"
        b"Requires-Dist: certifi>=2026.10.19; extra == 'quality'\n"
        b"Requires-Dist: torch<3,>=2.13; extra == 'quality'\n"
        b"Requires-Dist: transformers<6,>=5.10; extra == 'quality'\n"
        b"\n"
        b'<img src="https://dariuszpanas.github.io/yaga/assets/branding/'
        b'isometric_terminal_y_logo_transparent.png" alt="YAGA terminal Y logo">'
    )


def test_wheel_metadata_must_advertise_the_locked_runtime(runtime_metadata: bytes) -> None:
    valid = runtime_metadata
    check_build.validate_wheel_metadata(valid)

    for malformed in (
        valid.replace(b"Requires-Dist: pydantic<3,>=2.14.2\n", b""),
        valid.replace(b"Requires-Dist: pydantic-versions<2,>=1.0.4\n", b""),
        valid.replace(b"pydantic<3,>=2.14.2", b"pydantic>=2.14.2"),
        valid.replace(b"pydantic-versions<2,>=1.0.4", b"pydantic-versions>=1.0.4"),
        valid.replace(b"Requires-Dist: pygments<3,>=2.20.0\n", b""),
        valid.replace(b"pygments<3,>=2.20.0", b"pygments<3,>=2.13.0"),
        valid.replace(b"pygments<3,>=2.20.0", b"pygments>=2.20.0"),
        valid.replace(b"pygments<3,>=2.20.0", b"pygments<3,>=2.20.0; extra == 'quality'"),
        valid.replace(b"Requires-Dist: pyyaml<7,>=6.0.3\n", b""),
        valid.replace(b"Requires-Dist: typer<1,>=0.27.2\n", b""),
        valid.replace(b"pyyaml<7,>=6.0.3", b"pyyaml>=6.0.3"),
        valid.replace(b"typer<1,>=0.27.2", b"typer>=0.27.2"),
        valid.replace(b"torch<3,>=2.13", b"torch<3,>=2.6"),
        valid.replace(b"Requires-Dist: certifi>=2026.10.19; extra == 'quality'\n", b""),
        valid.replace(b"certifi>=2026.10.19", b"certifi>=2026.7.22"),
        valid.replace(b"certifi>=2026.10.19; extra == 'quality'", b"certifi>=2026.10.19"),
        valid.replace(
            b"Requires-Dist: typer<1,>=0.27.2",
            b"Requires-Dist: typer<1,>=0.27.2\nRequires-Dist: requests>=2",
        ),
        valid.replace(b"Requires-Python: >=3.12", b"Requires-Python: >=3.11"),
        valid.replace(b"Description-Content-Type: text/markdown\n", b""),
        valid.replace(b"text/markdown", b"text/plain"),
    ):
        with pytest.raises(SystemExit):
            check_build.validate_wheel_metadata(malformed)


def _sdist_metadata_archive(
    metadata: bytes,
    *,
    names: tuple[str, ...] = ("yaga_cli-0.0.0/PKG-INFO",),
    entry_type: bytes = tarfile.REGTYPE,
) -> BytesIO:
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name in names:
            member = tarfile.TarInfo(name)
            member.type = entry_type
            member.linkname = "outside-metadata"
            member.size = len(metadata) if member.isfile() else 0
            archive.addfile(member, BytesIO(metadata) if member.isfile() else None)
    stream.seek(0)
    return stream


def test_sdist_metadata_enforces_the_same_runtime_floor(runtime_metadata: bytes) -> None:
    with tarfile.open(fileobj=_sdist_metadata_archive(runtime_metadata)) as archive:
        check_build.validate_sdist_metadata(archive)
    weakened = runtime_metadata.replace(b"pygments<3,>=2.20.0", b"pygments<3,>=2.13.0")
    with tarfile.open(fileobj=_sdist_metadata_archive(weakened)) as archive:
        with pytest.raises(SystemExit, match="runtime dependency metadata"):
            check_build.validate_sdist_metadata(archive)


@pytest.mark.parametrize(
    "names",
    [(), ("yaga_cli-0.0.0/PKG-INFO",) * 2, ("yaga_cli-0.0.0/nested/PKG-INFO",)],
)
def test_sdist_metadata_must_be_unique_at_the_root(
    runtime_metadata: bytes, names: tuple[str, ...]
) -> None:
    with tarfile.open(fileobj=_sdist_metadata_archive(runtime_metadata, names=names)) as archive:
        with pytest.raises(SystemExit, match="metadata"):
            check_build.validate_sdist_metadata(archive)


@pytest.mark.parametrize("entry_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE])
def test_sdist_metadata_must_not_follow_links_or_directories(
    runtime_metadata: bytes, entry_type: bytes
) -> None:
    with tarfile.open(
        fileobj=_sdist_metadata_archive(runtime_metadata, entry_type=entry_type)
    ) as archive:
        with pytest.raises(SystemExit, match="regular root file"):
            check_build.validate_sdist_metadata(archive)


def test_sdist_metadata_has_a_byte_limit() -> None:
    oversized = b"x" * (check_build.MAX_DISTRIBUTION_METADATA_BYTES + 1)
    with tarfile.open(fileobj=_sdist_metadata_archive(oversized)) as archive:
        with pytest.raises(SystemExit, match="bounded"):
            check_build.validate_sdist_metadata(archive)


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
        *(ROOT / "src" / "yaga" / "agent_review" / "github").glob("*.py"),
    ]

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "import typer" not in source, path
        assert "from yaga.cli" not in source, path
        assert "yaga.cli_action" not in source, path
        assert "from yaga.changes" not in source, path
        assert "from yaga.branches" not in source, path
        assert "from yaga.descriptions" not in source, path
        assert "from yaga.trees" not in source, path
        assert "from yaga.sizes" not in source, path
        assert "from yaga.paths" not in source, path
        assert "from yaga.test_planning" not in source, path
        assert "from yaga.commands" not in source, path
        assert "from yaga.commits" not in source, path
        assert "from yaga.workflows" not in source, path
        assert "import yaga.changes" not in source, path
        assert "import yaga.branches" not in source, path
        assert "import yaga.descriptions" not in source, path
        assert "import yaga.trees" not in source, path
        assert "import yaga.sizes" not in source, path
        assert "import yaga.paths" not in source, path
        assert "import yaga.test_planning" not in source, path


def test_root_action_publishes_bounded_sanitized_step_summaries_in_every_mode() -> None:
    parsed = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
    run_steps = {step["id"]: step["run"] for step in parsed["runs"]["steps"] if "run" in step}

    assert set(run_steps) == {"banner", "invalid_check", "uv_runtime", "commit", "gate", "cli"}
    assert f"version:  {VERSION}" in run_steps["banner"]
    assert f"`{VERSION}`" in run_steps["invalid_check"]
    assert "GITHUB_STEP_SUMMARY" in run_steps["invalid_check"]
    assert "exit-code=2" in run_steps["invalid_check"]
    assert "yaga.step_summary" in run_steps["commit"]
    assert "yaga.step_summary" in run_steps["cli"]
    for step_id in ("commit", "gate", "cli"):
        script = run_steps[step_id]
        assert f"`{VERSION}`" in script
        assert "GITHUB_STEP_SUMMARY" in script
        assert "YAGA_REPORT" in script
        assert "YAGA_ERROR" in script
        assert "head -c 65536" in script
        assert "<pre>" in script
        assert "sed -e 's/&/\\&amp;/g'" in script
        assert 'exit "$YAGA_STATUS"' in script
        assert r"exit-code=%s\n" in script
        assert '>> "$GITHUB_OUTPUT"' in script


def test_root_action_marketplace_metadata_and_banner_sanitization() -> None:
    parsed = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
    assert parsed["name"] == "YAGA Checks"
    assert 1 <= len(parsed["description"]) <= 125
    assert parsed["branding"] == {"icon": "shield", "color": "green"}

    banner_step = parsed["runs"]["steps"][0]
    assert banner_step["id"] == "banner"
    assert "GITHUB_TOKEN" not in banner_step.get("env", {})
    assert "GH_TOKEN" not in banner_step.get("env", {})

    bash = "C:/Program Files/Git/bin/bash.exe" if sys.platform == "win32" else shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash is unavailable")

    plain = subprocess.run(
        [bash, "-c", banner_step["run"]],
        env={
            **os.environ,
            "NO_COLOR": "1",
            "YAGA_BANNER_CHECK": "gate",
            "YAGA_BANNER_OPERATION": "prepare",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=15,
    )
    assert "Y A G A   C H E C K S" in plain.stdout
    assert "Yet Another Gate Action" in plain.stdout
    assert "Automate the next gate." in plain.stdout
    assert "@@@@@@@%..%%=..=####=...=@@%++++++++%@@@@@@@" in plain.stdout
    assert f"> version:  {VERSION}" in plain.stdout
    assert "> check:    gate (prepare)" in plain.stdout
    assert "\x1b[" not in plain.stdout

    for mode in (
        "commit",
        "policy",
        "pr-review",
        "pr-description",
        "repo",
        "branch",
        "change",
        "workflow",
        "workflow-security",
        "workflow-lint",
        "mode",
        "path",
        "size",
        "tree",
    ):
        mode_run = subprocess.run(
            [bash, "-c", banner_step["run"]],
            env={
                **os.environ,
                "NO_COLOR": "1",
                "YAGA_BANNER_CHECK": mode,
                "YAGA_BANNER_OPERATION": "",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=15,
        )
        assert f"> check:    {mode}" in mode_run.stdout
        assert "> check:    unsupported" not in mode_run.stdout

    colored = subprocess.run(
        [bash, "-c", banner_step["run"]],
        env={k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        | {"YAGA_BANNER_CHECK": "pr-review", "YAGA_BANNER_OPERATION": ""},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=15,
    )
    assert "\x1b[38;2;131;188;140m" in colored.stdout
    assert "pr-review" in colored.stdout
    for line in colored.stdout.splitlines():
        if line:
            assert line.endswith("\x1b[0m")

    untrusted = subprocess.run(
        [bash, "-c", banner_step["run"]],
        env={
            **os.environ,
            "NO_COLOR": "1",
            "YAGA_BANNER_CHECK": "\x1b[31m$(whoami)",
            "YAGA_BANNER_OPERATION": "bad-op",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=15,
    )
    assert "> check:    unsupported" in untrusted.stdout
    assert "whoami" not in untrusted.stdout
    assert "bad-op" not in untrusted.stdout
    assert "\x1b[" not in untrusted.stdout


def test_commit_action_import_graph_is_dependency_free_and_read_only() -> None:
    paths = [
        ROOT / "src" / "yaga" / "commit_action_cli.py",
        ROOT / "src" / "yaga" / "commit_action_runtime.py",
        ROOT / "src" / "yaga" / "step_summary.py",
        ROOT / "src" / "yaga" / "files.py",
        ROOT / "src" / "yaga" / "policies" / "schemas.py",
        *(ROOT / "src" / "yaga" / "git").glob("*.py"),
        *(ROOT / "src" / "yaga" / "commits").glob("*.py"),
    ]

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "import typer" not in source, path
        assert "from yaga.cli" not in source, path
        assert "yaga.cli_action" not in source, path
        assert "from yaga.changes" not in source, path
        assert "from yaga.branches" not in source, path
        assert "from yaga.descriptions" not in source, path
        assert "from yaga.trees" not in source, path
        assert "from yaga.sizes" not in source, path
        assert "from yaga.paths" not in source, path
        assert "from yaga.test_planning" not in source, path
        assert "from yaga.commands" not in source, path
        assert "from yaga.agent_review.github" not in source, path
        assert "from yaga.workflows" not in source, path
        assert "from yaga.github import" not in source, path
        assert "import yaga.changes" not in source, path
        assert "import yaga.branches" not in source, path
        assert "import yaga.descriptions" not in source, path
        assert "import yaga.trees" not in source, path
        assert "import yaga.sizes" not in source, path
        assert "import yaga.paths" not in source, path
        assert "import yaga.test_planning" not in source, path
        assert "GITHUB_TOKEN" not in source, path


@pytest.mark.parametrize(
    "mutation",
    [
        "build-range",
        "dev-hatchling-mismatch",
        "dev-pip-audit-range",
        "lock-hatchling-version",
        "lock-pip-audit-version",
        "lock-sdist-hash",
        "lock-wheel-hash",
        "lock-requires-dev",
    ],
)
def test_supply_chain_pin_validation_rejects_unpinned_or_drifted_inputs(mutation: str) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    project = copy.deepcopy(project)
    lock = copy.deepcopy(lock)
    packages = {entry["name"]: entry for entry in lock["package"]}

    if mutation == "build-range":
        project["build-system"]["requires"] = ["hatchling>=1.33.0"]
    elif mutation == "dev-hatchling-mismatch":
        project["dependency-groups"]["dev"][0] = "hatchling==1.32.4"
    elif mutation == "dev-pip-audit-range":
        project["dependency-groups"]["dev"][1] = "pip-audit>=2.10.1"
    elif mutation == "lock-hatchling-version":
        packages["hatchling"]["version"] = "1.32.4"
    elif mutation == "lock-pip-audit-version":
        packages["pip-audit"]["version"] = "2.10.0"
    elif mutation == "lock-sdist-hash":
        packages["hatchling"]["sdist"]["hash"] = "sha256:bad"
    elif mutation == "lock-wheel-hash":
        packages["pip-audit"]["wheels"][0]["hash"] = "sha256:bad"
    else:
        packages["yaga-cli"]["metadata"]["requires-dev"]["dev"][0]["specifier"] = "==1.32.4"

    with pytest.raises(ValueError):
        validate_exact_supply_chain_pins(project, lock)


def test_pr_review_publisher_workflow_preserves_default_branch_trust_boundary() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "pr-review.yml").read_text("utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    assert set(triggers) == {"pull_request_target", "workflow_run"}
    assert triggers["pull_request_target"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "ready_for_review",
    ]
    assert triggers["workflow_run"] == {
        "workflows": ["CI", "Pull Request Review Wake"],
        "types": ["completed"],
    }
    assert workflow["permissions"] == {"contents": "read"}
    resolve_job = workflow["jobs"]["resolve"]
    assert resolve_job["permissions"] == {}
    job = workflow["jobs"]["publish-review"]
    assert job["needs"] == ["resolve"]
    assert job["concurrency"]["cancel-in-progress"] == (
        "${{ github.event_name == 'pull_request_target' }}"
    )
    assert job["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
        "issues": "read",
        "checks": "read",
    }
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"persist-credentials": False}
    action_step = job["steps"][1]
    assert action_step["uses"] == "./"
    assert action_step["with"] == {
        "check": "pr-review",
        "policy": ".yaga/agent-review-policy.toml",
        "pr": "${{ needs.resolve.outputs.pull_request_number }}",
        "repo": "${{ github.repository }}",
        "wait-seconds": "${{ needs.resolve.outputs.wait_seconds }}",
        "submit-review": "both",
        "github-token": "${{ secrets.GITHUB_TOKEN }}",
    }

    wake_workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "pr-review-wake.yml").read_text("utf-8")
    )
    wake_triggers = wake_workflow.get("on", wake_workflow.get(True))
    assert isinstance(wake_triggers, dict)
    assert set(wake_triggers) == {"issue_comment", "pull_request_review", "workflow_dispatch"}
    assert wake_triggers["pull_request_review"] == {"types": ["submitted"]}
    assert wake_workflow["permissions"] == {}
    assert wake_workflow["jobs"]["wake"]["permissions"] == {}
