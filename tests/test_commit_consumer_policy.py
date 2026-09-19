"""Opt-in consumer policy boundaries shared by CLI and Action checks."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from yaga.commits.checker import check_header, check_target
from yaga.commits.config import load_config
from yaga.commits.models import CommitPolicy, CommitTarget
from yaga.errors import ConfigurationError

POLICY = """config-version = 1
[commit]
allowed-types = ["build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test"]
type-case = "lower"
header-max-length = 72
description-min-length = 10
description-case = "forbid-initial-uppercase"
length-unit = "utf16"
footer-syntax = "colon-whitespace"
description-ending = "forbid-period"
body-policy = "required"
body-min-length = 100
body-max-line-length = 72
footer-max-line-length = 100
line-length-urls = "exempt"
required-colon-footer-tokens = ["Validation"]
"""
BODY = (
    "Keep lease ownership attached to the active worker generation so stale\n"
    "workers cannot publish a late result after replacement. This preserves\n"
    "deterministic recovery across all supported execution backends."
)
HEADER = "fix(worker): preserve bounded lease ownership"


def message(
    header: str = HEADER, body: str = BODY, footer: str = "Validation: checks passed."
) -> str:
    return f"{header}\n\n{body}\n\n{footer}"


@pytest.fixture
def policy(tmp_path: Path) -> CommitPolicy:
    path = tmp_path / ".yaga.toml"
    path.write_text(POLICY, encoding="utf-8")
    return load_config(path).policy


@pytest.mark.parametrize(
    ("text", "valid"),
    [
        (message(), True),
        (HEADER, False),
        (message(body="short body"), False),
        (message(footer=""), False),
        (message(footer="Validation:"), False),
        (message(footer="validation: checks passed."), False),
        (message(footer="Validation #123"), False),
        (message(footer="Validation:\tchecks passed."), True),
        (message(footer="Validation:   checks passed."), True),
        (message(footer="Validation: " + "x" * 101), False),
        (message(body=BODY + "\n" + "x" * 73), False),
        (message(body=BODY + "\nhttps://example.com/" + "x" * 100), True),
        (message(footer="Validation: https://example.com/" + "x" * 100), True),
        (message(header="fix: Preserve bounded lease ownership"), False),
        (message(header="fix: Preserve Bounded Lease Ownership"), False),
        (message(header="fix: PreserveBoundedLeaseOwnership"), False),
        (message(header="fix: PRESERVE BOUNDED LEASE OWNERSHIP"), False),
        (message(header=HEADER + "."), False),
        (message(header=HEADER + "?"), True),
        (message(header=HEADER + "!"), True),
        (message(header=HEADER.replace("): ", ")!: ")), True),
        (message(footer="BREAKING CHANGE: remove legacy ownership.\nValidation: checked."), True),
        (message(header="fixup! " + HEADER), False),
        (message(header='Revert "' + HEADER + '"'), False),
        (message(header="unknown: preserve bounded lease ownership"), False),
    ],
)
def test_consumer_message_boundaries(policy: CommitPolicy, text: str, valid: bool) -> None:
    result = check_target(CommitTarget("fixture", text), policy)
    assert result.valid == valid, result.diagnostics


def test_title_only_boundary(policy: CommitPolicy) -> None:
    assert check_header(CommitTarget("title", HEADER), policy).valid
    assert not check_header(CommitTarget("title", HEADER + "."), policy).valid


def test_unicode_length_and_case_boundaries(policy: CommitPolicy) -> None:
    for initial, valid in [("É", False), ("é", True), ("ǅ", True), ("🤖", True)]:
        result = check_header(
            CommitTarget("title", "fix: " + initial + " preserve ownership"), policy
        )
        assert result.valid == valid
    header = "fix: " + "🤖" * 34
    assert not check_header(CommitTarget("title", header), policy).valid
    codepoints = replace(policy, length_unit=CommitPolicy().length_unit)
    assert check_header(CommitTarget("title", header), codepoints).valid


def test_recorded_commitlint_reference_corpus(policy: CommitPolicy) -> None:
    document = json.loads(
        (Path(__file__).parent / "fixtures/commit_consumer_parity.json").read_text("utf-8")
    )
    for case in document["cases"]:
        result = check_target(CommitTarget("reference", case["message"]), policy)
        assert result.valid == case["valid"], (case, result.diagnostics)


def test_defaults_remain_unchanged(policy: CommitPolicy) -> None:
    assert check_target(CommitTarget("fixture", message(header=HEADER + ".")), CommitPolicy()).valid
    no_exemption = replace(policy, line_length_urls=CommitPolicy().line_length_urls)
    result = check_target(
        CommitTarget("fixture", message(body=BODY + "\nhttps://x/" + "x" * 100)), no_exemption
    )
    assert any(d.code == "body.line-length" for d in result.diagnostics)


@pytest.mark.parametrize(
    "setting",
    [
        'description-case = "sentence"',
        'length-unit = "bytes"',
        "footer-syntax = true",
        "line-length-urls = true",
        "footer-max-line-length = true",
        "footer-max-line-length = 0",
        'required-colon-footer-tokens = ["Validation", "validation"]',
        'required-colon-footer-tokens = ["Validation:"]',
        'required-colon-footer-tokens = ["BREAKING-CHANGE"]',
        'required-colon-footer-tokens = ["Validation"]\nforbidden-footer-tokens = ["validation"]',
    ],
)
def test_invalid_consumer_settings(tmp_path: Path, setting: str) -> None:
    path = tmp_path / ".yaga.toml"
    path.write_text("[commit]\n" + setting, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(path)
