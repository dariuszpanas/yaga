"""Authoring help stays read-only, bounded, and deliberately incomplete."""

from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commits.checker import check_target
from yaga.commits.models import CommitPolicy, CommitTarget, MessageFormat, PresencePolicy
from yaga.commits.template import TemplateComment, message_template
from yaga.errors import InputError


def test_template_requires_authored_description_and_never_claims_validation() -> None:
    policy = CommitPolicy(
        body_policy_by_type=(("fix", PresencePolicy.REQUIRED),),
        required_footer_tokens=("Validation",),
        footer_values=(("Validation", ("passed", "not-run")),),
    )
    output = message_template(policy, commit_type="fix")
    assert output.startswith("fix: \n\n")
    assert "# Body policy: required." in output and "Validation: \n" in output
    assert "\nValidation: passed" not in output
    assert not check_target(CommitTarget("template", output), policy).valid


def test_template_respects_comment_character_and_plain_format() -> None:
    policy = CommitPolicy(message_format=MessageFormat.PLAIN)
    output = message_template(policy, comment=TemplateComment.SEMICOLON)
    assert output.startswith("\n\n; ") and "\n# " not in output
    assert not check_target(CommitTarget("template", output), policy).valid
    with pytest.raises(InputError, match="plain"):
        message_template(policy, commit_type="fix")


@pytest.mark.parametrize("scope", ["", "x" * 129, "bad\x1b", "a b", "a): injected\n\nfix(b"])
def test_template_scope_cannot_inject_message_structure(scope: str) -> None:
    with pytest.raises(InputError):
        message_template(CommitPolicy(ignored_headers=("*",)), commit_type="fix", scope=scope)


def test_component_requirements_cannot_be_waived_by_ignored_headers() -> None:
    policy = CommitPolicy(
        allowed_types=("fix",), scope_policy=PresencePolicy.REQUIRED, ignored_headers=("*",)
    )
    with pytest.raises(InputError):
        message_template(policy, commit_type="docs", scope="core")
    with pytest.raises(InputError):
        message_template(policy, commit_type="fix")
    assert message_template(policy, commit_type="fix", scope="core").startswith("fix(core): ")
    with pytest.raises(InputError):
        message_template(
            replace(policy, scope_policy=PresencePolicy.FORBIDDEN), commit_type="fix", scope="core"
        )


def test_template_command_does_not_write_files_or_edit_configuration(tmp_path: Path) -> None:
    path = tmp_path / ".yaga.toml"
    config = '[commit]\nmessage-format="plain"\n'
    path.write_text(config, encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    runner = CliRunner()
    result = runner.invoke(app, ["commit", "template", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("\n\n# ")
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    show = runner.invoke(app, ["config", "show", "--config", str(path), "--type", "fix"])
    assert show.exit_code == 2 and "no commit type" in show.output


def test_template_respects_nonblocking_component_policy() -> None:
    from yaga.commits.models import CasePolicy

    policy = CommitPolicy(type_case=CasePolicy.LOWER, warning_rules=("type.case",))
    output = message_template(policy, commit_type="Fix")
    assert output.startswith("Fix: ") and "Warning-only rules: type.case" in output
