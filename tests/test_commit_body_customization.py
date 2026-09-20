"""Project-specific prose requirements without blanket commit exemptions."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commits.checker import check_header, check_target
from yaga.commits.config import load_config
from yaga.commits.models import CommitTarget
from yaga.commits.reporting import policy_document
from yaga.errors import ConfigurationError


def load(tmp_path: Path, settings: str):
    path = tmp_path / ".yaga.toml"
    path.write_text("config-version = 1\n[commit]\n" + settings, encoding="utf-8")
    return load_config(path).policy


def codes(policy, text: str):
    return [item.code for item in check_target(CommitTarget("example", text), policy).diagnostics]


def test_project_can_require_explanations_only_for_selected_types(tmp_path: Path) -> None:
    policy = load(tmp_path, 'body-policy-by-type = {feat="required", fix="required"}\n')
    assert codes(policy, "feat: add option") == ["body.required"]
    assert codes(policy, "FIX: correct behavior") == ["body.required"]
    assert codes(policy, "docs: correct wording") == []
    assert codes(policy, "feat: add option\n\nExplain why this helps.") == []
    assert check_header(CommitTarget("title", "feat: add option"), policy).valid
    assert policy_document(policy)["body_policy_by_type"] == {"feat": "required", "fix": "required"}


def test_override_does_not_skip_other_checks_or_require_optional_body(tmp_path: Path) -> None:
    policy = load(
        tmp_path,
        'body-policy="required"\nbody-min-length=10\n'
        'description-ending="forbid-period"\n'
        'body-policy-by-type={docs="optional", chore="forbidden"}\n',
    )
    assert codes(policy, "docs: correct wording") == []
    assert codes(policy, "docs: correct wording.") == ["description.ending"]
    assert codes(policy, "docs: correct wording\n\nShort") == ["body.length"]
    assert codes(policy, "chore: update metadata") == []
    assert codes(policy, "chore: update metadata\n\nShort") == ["body.forbidden"]
    assert codes(policy, "fix: correct behavior") == ["body.required"]


def test_global_forbidden_can_have_reachable_required_override(tmp_path: Path) -> None:
    policy = load(
        tmp_path,
        'body-policy="forbidden"\nbody-min-length=10\nbody-policy-by-type={fix="required"}\n',
    )
    assert codes(policy, "fix: correct behavior") == ["body.required"]
    assert codes(policy, "fix: correct behavior\n\nLong enough prose") == []
    assert codes(policy, "docs: correct wording") == []


@pytest.mark.parametrize(
    "settings",
    [
        "body-policy-by-type=[]",
        "body-policy-by-type={fix=true}",
        'body-policy-by-type={fix="ignore"}',
        'body-policy-by-type={fix="required", FIX="optional"}',
        'body-policy-by-type={"bad type"="required"}',
        'allowed-types=["feat"]\nbody-policy-by-type={fix="required"}',
        'allowed-types=["fix"]\nbody-min-length=1\nbody-policy-by-type={fix="forbidden"}',
        "body-max-length=0",
        "body-max-length=true",
        "body-max-length=100001",
        "body-min-length=20\nbody-max-length=10",
        "body-min-words=3\nbody-max-length=4",
    ],
)
def test_invalid_body_configuration_fails_early(tmp_path: Path, settings: str) -> None:
    with pytest.raises(ConfigurationError):
        load(tmp_path, settings)


def test_override_count_bound(tmp_path: Path) -> None:
    values = ",".join(f't{i}="optional"' for i in range(129))
    with pytest.raises(ConfigurationError, match="128"):
        load(tmp_path, f"body-policy-by-type={{{values}}}")


def test_word_minimum_can_reach_exact_body_maximum(tmp_path: Path) -> None:
    policy = load(tmp_path, "body-min-words=3\nbody-max-length=5")
    assert codes(policy, "fix: correct behavior\n\na b c") == []


@pytest.mark.parametrize(
    ("unit", "body", "expected"),
    [
        ("codepoints", "12345", []),
        ("codepoints", "123456", ["body.length"]),
        ("codepoints", "1234😀", []),
        ("utf16", "1234😀", ["body.length"]),
        ("codepoints", "12\n345", ["body.length"]),
    ],
)
def test_body_limit_counts_prose_in_configured_units(tmp_path: Path, unit, body, expected) -> None:
    policy = load(tmp_path, f'body-max-length=5\nlength-unit="{unit}"\n')
    assert codes(policy, f"fix: correct behavior\n\n{body}") == expected
    assert codes(policy, "fix: correct behavior\n\nValidation: a long footer") == []
    assert codes(policy, "fix: correct behavior") == []


def test_public_cli_applies_project_overrides(tmp_path: Path) -> None:
    load(tmp_path, 'body-policy-by-type={fix="required"}\nbody-max-length=8\n')
    base = ["commit", "check", "--repo", str(tmp_path)]
    runner = CliRunner()
    assert runner.invoke(app, [*base, "--message", "fix: correct behavior"]).exit_code == 1
    assert runner.invoke(app, [*base, "--message", "docs: correct wording"]).exit_code == 0
    assert (
        runner.invoke(
            app, [*base, "--message", "fix: correct behavior\n\nToo much prose"]
        ).exit_code
        == 1
    )
    assert runner.invoke(app, [*base, "--title", "fix: correct behavior"]).exit_code == 0
