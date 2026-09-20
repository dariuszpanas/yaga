"""Explicit ordinary messages preserve common policy and reject meaningless components."""

from dataclasses import replace
from pathlib import Path

import pytest

from yaga.commits.checker import check_header, check_target
from yaga.commits.config import load_config
from yaga.commits.models import CommitPolicy, CommitTarget, MessageFormat, PresencePolicy
from yaga.commits.parser import parse_message, parse_plain_message
from yaga.errors import ConfigurationError

PLAIN = CommitPolicy(message_format=MessageFormat.PLAIN)


def test_plain_parser_does_not_infer_conventional_components() -> None:
    parsed = parse_plain_message("fix(core)!: ordinary words\n\nExplain why\n\nRefs: #12")
    assert parsed is not None
    assert parsed.commit_type is None and parsed.scope is None and not parsed.breaking_header
    assert parsed.description == "fix(core)!: ordinary words"
    assert parsed.body == "Explain why" and parsed.footer_lines == ("Refs: #12",)
    assert parse_message("Ordinary title") is None
    assert check_target(CommitTarget("message", "Ordinary title"), PLAIN).valid
    assert not check_target(CommitTarget("message", "Ordinary title"), CommitPolicy()).valid


@pytest.mark.parametrize(
    "title",
    [
        "",
        " ",
        " leading",
        "trailing ",
        "bad\tcontrol",
        "bad\x1bcontrol",
        "bad\u202econtrol",
        "bad\u2028line",
    ],
)
def test_plain_header_structure_remains_strict(title: str) -> None:
    assert parse_plain_message(title) is None
    assert not check_target(CommitTarget("message", title), PLAIN).valid


def test_plain_policy_applies_body_footer_reference_and_severity() -> None:
    policy = replace(
        PLAIN,
        body_policy=PresencePolicy.REQUIRED,
        required_footer_tokens=("Validation",),
        footer_values=(("Validation", ("passed",)),),
        required_issue_prefixes=("#",),
        warning_rules=("body.required",),
    )
    result = check_target(
        CommitTarget("message", "Correct behavior\n\nRefs: #12\nValidation: passed"), policy
    )
    assert result.valid and [d.code for d in result.diagnostics] == ["body.required"]
    assert check_header(CommitTarget("title", "Correct behavior"), policy).valid
    invalid = check_target(CommitTarget("message", "Correct behavior\nNo separator"), policy)
    assert not invalid.valid and "syntax.separator" in {d.code for d in invalid.diagnostics}


def test_plain_description_limits_measure_the_whole_header(tmp_path: Path) -> None:
    path = tmp_path / ".yaga.toml"
    path.write_text(
        '[commit]\nmessage-format="plain"\nheader-max-length=1\ndescription-min-length=1\n',
        encoding="utf-8",
    )
    policy = load_config(path).policy
    assert check_target(CommitTarget("message", "x"), policy).valid
    assert [d.code for d in check_target(CommitTarget("message", "xy"), policy).diagnostics] == [
        "header.length"
    ]


@pytest.mark.parametrize(
    "setting",
    [
        'allowed-types=["fix"]',
        'type-case="any"',
        'scope-policy="optional"',
        "allowed-scopes=[]",
        'scope-case="any"',
        "scope-policy-by-type={}",
        "body-policy-by-type={}",
        'breaking-markers="either"',
        'warning-rules=["scope.required"]',
    ],
)
def test_plain_mode_rejects_conventional_only_configuration(tmp_path: Path, setting: str) -> None:
    path = tmp_path / ".yaga.toml"
    path.write_text('[commit]\nmessage-format="plain"\n' + setting, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="plain message-format"):
        load_config(path)


@pytest.mark.parametrize("value", ['"auto"', "true", "1", "[]"])
def test_message_format_is_closed(tmp_path: Path, value: str) -> None:
    path = tmp_path / ".yaga.toml"
    path.write_text("[commit]\nmessage-format=" + value, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(path)
