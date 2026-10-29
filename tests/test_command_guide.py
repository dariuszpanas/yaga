"""Exercise copied onboarding examples and keep the command inventory complete."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
from typer.core import TyperGroup
from typer.main import get_command
from typer.testing import CliRunner

from yaga.cli import app
from yaga.repository.models import RepositoryProvider
from yaga.repository.plan import load_repository_check_plan

ROOT = Path(__file__).parents[1]
runner = CliRunner()


def _leaf_commands(command: object, prefix: str = "") -> set[str]:
    if not isinstance(command, TyperGroup):
        return {prefix}
    return {
        leaf
        for name, child in command.commands.items()
        for leaf in _leaf_commands(child, f"{prefix} {name}".strip())
    }


def test_command_guide_covers_every_installed_checker() -> None:
    document = (ROOT / "docs/commands.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| \[`yaga ([^`]+)`\]\(", document, flags=re.MULTILINE))
    commands = _leaf_commands(get_command(app))
    helpers = {"commit template", "config init", "config show", "self update"}
    expected = {
        "gate agent-review <operation>"
        if name.startswith("gate agent-review ") and " policy " not in name
        else name
        for name in commands - helpers
    }
    assert documented == expected


def test_configuration_guide_plan_loads_and_checks_workflow(tmp_path: Path) -> None:
    document = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    block = re.search(r"```toml\n(# workflow-checks.toml\n.*?)```", document, flags=re.DOTALL)
    assert block is not None
    plan_path = tmp_path / "workflow-checks.toml"
    plan_path.write_text(block.group(1), encoding="utf-8")
    plan = load_repository_check_plan(plan_path)
    assert plan.plan_version == 2
    assert plan.checks == (RepositoryProvider.WORKFLOW,)
    _write_workflow(tmp_path)
    result = runner.invoke(
        app, ["repo", "check", "--repo", str(tmp_path), "--plan", str(plan_path)]
    )
    assert result.exit_code == 0, result.output


def _write_workflow(repository: Path) -> None:
    directory = repository / ".github/workflows"
    directory.mkdir(parents=True)
    (directory / "ci.yml").write_text(
        "name: CI\non: push\npermissions:\n  contents: read\njobs:\n"
        "  check:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo checked\n",
        encoding="utf-8",
    )


def test_before_commit_recipe_checks_prospective_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".yaga").mkdir()
    (tmp_path / ".yaga.toml").write_text("config-version = 1\n", encoding="utf-8")
    (tmp_path / ".yaga/branch-policy.toml").write_text(
        'branch-policy-version = 1\nallowed-patterns = ["feat/*"]\n', encoding="utf-8"
    )
    (tmp_path / ".yaga/description-policy.toml").write_text(
        "description-policy-version = 1\n", encoding="utf-8"
    )
    (tmp_path / "message.txt").write_text(
        "feat(parser): explain validation\n\nReject malformed inputs before applying policy.\n",
        encoding="utf-8",
    )
    (tmp_path / "pr-description.md").write_text(
        "Explain the parser change and its validation.\n", encoding="utf-8"
    )
    _write_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    document = (ROOT / "docs/commands.md").read_text(encoding="utf-8")
    section = document.split("### Before creating a commit\n", 1)[1].split("### ", 1)[0]
    block = re.search(r"```bash\n(.*?)```", section, flags=re.DOTALL)
    assert block is not None
    commands = block.group(1).strip().splitlines()
    assert len(commands) == 5
    for command in commands:
        executable, *arguments = shlex.split(command)
        assert executable == "yaga"
        result = runner.invoke(app, arguments)
        assert result.exit_code == 0, f"{command}\n{result.output}"


def test_review_action_documented_inputs_match_manifest() -> None:
    manifest = (ROOT / "action.yml").read_text(encoding="utf-8")
    inputs = manifest.split("\ninputs:\n", 1)[1].split("\noutputs:\n", 1)[0]
    expected = set(re.findall(r"^  ([a-z][a-z-]+):", inputs, flags=re.MULTILINE))
    guide = (ROOT / "docs/github-review-adapter.md").read_text(encoding="utf-8")
    interface = guide.split("## Action interface\n", 1)[1].split("\n## ", 1)[0]
    assert set(re.findall(r"^\| `([a-z][a-z-]+)` \|", interface, flags=re.MULTILINE)) == expected
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    declaration = contributing.split("The public inputs are exactly ", 1)[1].split(".", 1)[0]
    assert set(re.findall(r"`([a-z][a-z-]+)`", declaration)) == expected
