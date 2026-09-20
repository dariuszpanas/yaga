"""Configured references and footer enums check syntax without claiming external truth."""

from dataclasses import replace
from pathlib import Path

import pytest

from yaga.commits.checker import check_header, check_target
from yaga.commits.config import load_config
from yaga.commits.content_policy import has_issue_reference
from yaga.commits.models import CommitPolicy, CommitTarget
from yaga.errors import ConfigurationError


@pytest.mark.parametrize(
    "text", ["#1", "(#123).", "PROJ-27", "Refs: #123", "see\nPROJ-9", "#99999999999999999999"]
)
def test_reference_tokens(text: str) -> None:
    assert has_issue_reference(text, ("#", "PROJ-"))


@pytest.mark.parametrize(
    "text",
    [
        "#0",
        "#01",
        "#-1",
        "#١",
        "#１２",
        "#1suffix",
        "prefix#1",
        "proj-1",
        "#" + "1" * 21,
        "https://example.com/#1",
        "PROJ-",
        "PROJ- 12",
    ],
)
def test_reference_near_misses(text: str) -> None:
    assert not has_issue_reference(text, ("#", "PROJ-"))


def test_reference_prefix_overlap_and_long_nonmatching_input() -> None:
    assert has_issue_reference("AB12", ("A", "AB", "AB1"))
    assert not has_issue_reference("x" * 1_000_000, tuple(f"P{i}-" for i in range(128)))


def test_full_message_reference_requirement_does_not_apply_to_title() -> None:
    policy = CommitPolicy(required_issue_prefixes=("#",))
    target = CommitTarget("message", "fix: correct behavior")
    assert [d.code for d in check_target(target, policy).diagnostics] == ["reference.required"]
    assert check_header(target, policy).valid
    assert check_target(replace(target, message=target.message + "\n\nRefs: #12"), policy).valid


@pytest.mark.parametrize(
    "suffix,valid",
    [
        ("Validation: passed", True),
        ("validation: passed", True),
        ("Validation: Passed", False),
        ("Validation: TODO", False),
        ("Validation: passed\n\nextra prose", False),
        ("Validation: passed\nRefs: #12", True),
        ("Validation: passed\nValidation: TODO", False),
        ("Validation: TODO\nValidation: passed", False),
        ("Validation: passed  ", True),
        ("", True),
    ],
)
def test_footer_values_are_exact_and_check_every_occurrence(suffix: str, valid: bool) -> None:
    policy = CommitPolicy(footer_values=(("Validation", ("passed", "not-run")),))
    result = check_target(CommitTarget("message", "fix: correct behavior\n\n" + suffix), policy)
    assert result.valid is valid
    if not valid:
        assert [d.code for d in result.diagnostics] == ["footer.value"]
        assert result.diagnostics[0].line >= 3


def test_footer_constraints_require_recognized_footer_boundaries() -> None:
    policy = CommitPolicy(footer_values=(("Validation", ("passed",)),))
    target = CommitTarget("message", "fix: correct behavior\n\nProse\nValidation: TODO")
    assert check_target(target, policy).valid
    required = replace(policy, required_footer_tokens=("Validation",))
    assert [d.code for d in check_target(target, required).diagnostics] == ["footer.required"]
    assert check_header(CommitTarget("title", "fix: correct behavior"), required).valid


def test_hash_separator_and_colon_whitespace_policy() -> None:
    from yaga.commits.models import FooterSyntax

    policy = CommitPolicy(
        footer_values=(("Refs", ("123",)),), footer_syntax=FooterSyntax.COLON_WHITESPACE
    )
    assert check_target(CommitTarget("message", "fix: x\n\nRefs #123"), policy).valid
    assert check_target(CommitTarget("message", "fix: x\n\nRefs:\t123"), policy).valid


@pytest.mark.parametrize(
    "config",
    [
        'required-issue-prefixes=["#", "#"]',
        'required-issue-prefixes=["a.*"]',
        'required-issue-prefixes=["é-"]',
        "required-issue-prefixes=true",
        "footer-values={Validation=[]}",
        'footer-values={Validation=[" x"]}',
        'footer-values={Validation=["x", "x"]}',
        "footer-values={Validation=[1]}",
        'footer-values={Validation=["x"], validation=["y"]}',
        'footer-values={"BREAKING-CHANGE"=["x"]}',
        'footer-values={Validation=["x"]}\nforbidden-footer-tokens=["validation"]',
    ],
)
def test_invalid_content_configuration(tmp_path: Path, config: str) -> None:
    path = tmp_path / ".yaga.toml"
    path.write_text("[commit]\n" + config, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_configuration_roundtrip_and_bounds(tmp_path: Path) -> None:
    import json

    from yaga.commits.models import OutputFormat
    from yaga.commits.reporting import render_config

    path = tmp_path / ".yaga.toml"
    path.write_text(
        '[commit]\nrequired-issue-prefixes=["#", "PROJ-"]\nfooter-values={Validation=["passed", "not-run"]}',
        encoding="utf-8",
    )
    loaded = load_config(path)
    document = json.loads(render_config(loaded, OutputFormat.JSON))
    assert document["config"]["required_issue_prefixes"] == ["#", "PROJ-"]
    assert document["config"]["footer_values"] == {"Validation": ["passed", "not-run"]}
    for setting in (
        "required-issue-prefixes=" + json.dumps([f"P{i}-" for i in range(129)]),
        "required-issue-prefixes=" + json.dumps(["P" * 65]),
        "footer-values={V=" + json.dumps(["x" * 257]) + "}",
        "footer-values={" + ",".join(f'V{i}=["x"]' for i in range(129)) + "}",
        "footer-values={"
        + ",".join(f"V{i}=" + json.dumps([str(j) for j in range(100)]) for i in range(3))
        + "}",
    ):
        path.write_text("[commit]\n" + setting, encoding="utf-8")
        with pytest.raises(ConfigurationError):
            load_config(path)
