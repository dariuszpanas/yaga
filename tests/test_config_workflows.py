"""Workflow starters and type explanations preserve checked policy behavior."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commits.checker import check_header, check_target
from yaga.commits.config import load_config
from yaga.commits.config_explain import explain_type
from yaga.commits.config_init import ConfigStarter, initialize_config
from yaga.commits.models import CommitTarget, OutputFormat
from yaga.commits.reporting import render_config
from yaga.errors import ConfigurationError, InputError


@pytest.mark.parametrize("starter", list(ConfigStarter))
def test_starter_preview_and_created_policy_agree(tmp_path: Path, starter: ConfigStarter) -> None:
    preview = initialize_config(tmp_path, dry_run=True, starter=starter)
    assert not (tmp_path / ".yaga.toml").exists()
    created = initialize_config(tmp_path, starter=starter)
    assert preview.policy == created.policy
    with pytest.raises(ConfigurationError, match="already"):
        initialize_config(tmp_path, starter=starter)


def test_starters_distinguish_full_message_requirements(tmp_path: Path) -> None:
    title = initialize_config(tmp_path, dry_run=True, starter=ConfigStarter.TITLE_V1).policy
    complete = initialize_config(
        tmp_path, dry_run=True, starter=ConfigStarter.COMPLETE_MESSAGE_V1
    ).policy
    fix = CommitTarget("message", "fix: correct behavior")
    assert check_target(fix, title).valid
    assert not check_target(fix, complete).valid
    assert check_header(fix, complete).valid
    assert check_target(CommitTarget("message", "docs: clarify behavior"), complete).valid
    assert check_target(
        CommitTarget("message", "fix: correct behavior\n\nExplain why"), complete
    ).valid


def test_explanation_matches_checker_overrides_and_fallback(tmp_path: Path) -> None:
    config = tmp_path / ".yaga.toml"
    config.write_text(
        '[commit]\nallowed-types=["fix", "docs"]\nbody-policy="forbidden"\n'
        'body-policy-by-type={fix="required"}\nscope-policy="optional"\n'
        'scope-policy-by-type={fix="required"}\n',
        encoding="utf-8",
    )
    loaded = load_config(config)
    explanation = explain_type(loaded.policy, "FIX")
    assert explanation["body_policy"] == explanation["scope_policy"] == "required"
    assert explanation["body_source"] == "body-policy-by-type.fix"
    assert explanation["listed_type_allowed"]
    assert not check_target(CommitTarget("message", "FIX: correct behavior"), loaded.policy).valid
    assert check_target(
        CommitTarget("message", "FIX(core): correct behavior\n\nExplain why"), loaded.policy
    ).valid
    fallback = explain_type(loaded.policy, "docs")
    assert fallback["body_source"] == "body-policy" and fallback["body_policy"] == "forbidden"
    assert not explain_type(loaded.policy, "unknown")["listed_type_allowed"]
    assert "effective_type" not in json.loads(render_config(loaded, OutputFormat.JSON))
    rendered = json.loads(render_config(loaded, OutputFormat.JSON, commit_type="FIX"))
    assert rendered["effective_type"] == explanation
    assert "body-policy-by-type.fix" in render_config(loaded, OutputFormat.TEXT, commit_type="FIX")


@pytest.mark.parametrize(
    "value", ["", "fix: x", "fix(core)", "x" * 129, "fix\n", "fix\x1b", "\ud800"]
)
def test_explanation_rejects_unsafe_type_tokens(value: str) -> None:
    from yaga.commits.models import CommitPolicy

    with pytest.raises(InputError):
        explain_type(CommitPolicy(), value)


def test_cli_starter_and_type_explanation(tmp_path: Path) -> None:
    runner = CliRunner()
    preview = runner.invoke(
        app,
        [
            "config",
            "init",
            "--repo",
            str(tmp_path),
            "--starter",
            "complete-message-v1",
            "--dry-run",
            "--format",
            "json",
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["dry_run"]
    assert not (tmp_path / ".yaga.toml").exists()
    created = runner.invoke(
        app, ["config", "init", "--repo", str(tmp_path), "--starter", "complete-message-v1"]
    )
    assert created.exit_code == 0, created.output
    shown = runner.invoke(
        app, ["config", "show", "--repo", str(tmp_path), "--type", "fix", "--format", "json"]
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["effective_type"]["body_policy"] == "required"
    invalid = runner.invoke(app, ["config", "show", "--repo", str(tmp_path), "--type", "fix: bad"])
    assert invalid.exit_code == 2
    unknown = runner.invoke(app, ["config", "init", "--repo", str(tmp_path), "--starter", "latest"])
    assert unknown.exit_code == 2
