"""Tests for pure branch-name policy checking."""

from __future__ import annotations

from typing import cast

import pytest

from yaga.branches import checker as branch_checker
from yaga.branches.checker import check_branch_name
from yaga.branches.models import (
    BRANCH_ALLOWED_CODE,
    BRANCH_SYNTAX_CODE,
    MAX_BRANCH_COMPONENTS,
    MAX_BRANCH_NAME_BYTES,
    BranchPolicy,
)
from yaga.errors import InputError


def policy(*patterns: str) -> BranchPolicy:
    return BranchPolicy(1, patterns)


def test_checker_returns_exact_first_configured_match() -> None:
    broad_first = check_branch_name(policy("**", "feat/**"), "feat/topic")
    narrow_first = check_branch_name(policy("feat/**", "**"), "feat/topic")

    assert broad_first.matched_pattern == "**"
    assert narrow_first.matched_pattern == "feat/**"
    assert broad_first.diagnostics == ()
    assert broad_first.status == "passed"
    assert broad_first.valid


def test_checker_reports_one_allowed_finding_for_valid_unmatched_name() -> None:
    report = check_branch_name(policy("main", "feat/**"), "fix/topic")

    assert report.matched_pattern is None
    assert tuple(item.code for item in report.diagnostics) == (BRANCH_ALLOWED_CODE,)
    assert report.status == "failed"
    assert not report.valid


def test_syntax_finding_precedes_matching_and_never_interpolates_raw_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_match(pattern: str, branch: str) -> bool:
        raise AssertionError((pattern, branch))

    monkeypatch.setattr(branch_checker, "match_branch_pattern", fail_match)
    branch = "bad name\n"

    report = check_branch_name(policy("**"), branch)

    assert report.matched_pattern is None
    assert tuple(item.code for item in report.diagnostics) == (BRANCH_SYNTAX_CODE,)
    assert branch not in report.diagnostics[0].message


@pytest.mark.parametrize("branch", ["", "a" * (MAX_BRANCH_NAME_BYTES + 1), "\ud800"])
def test_transport_failures_are_input_errors_not_syntax_findings(branch: str) -> None:
    with pytest.raises(InputError, match="branch name"):
        check_branch_name(policy("**"), branch)


def test_transport_limit_counts_utf8_bytes_before_portable_syntax() -> None:
    exact_ascii = "a" * MAX_BRANCH_NAME_BYTES
    exact_unicode = "é" * (MAX_BRANCH_NAME_BYTES // 2)
    oversized_unicode = exact_unicode + "é"

    assert check_branch_name(policy("**"), exact_ascii).valid
    assert check_branch_name(policy("**"), exact_unicode).diagnostics[0].code == BRANCH_SYNTAX_CODE
    with pytest.raises(InputError, match="UTF-8 bytes"):
        check_branch_name(policy("**"), oversized_unicode)


@pytest.mark.parametrize(
    "branch",
    [
        "a",
        "Main",
        "feat/topic",
        "feat/HEAD",
        "HEAD/foo",
        "refs",
        "x/refs/y",
        "origin/feat/x",
        "a-b_c.d/0",
        "a--b__c",
        "feat/topic.locked",
        "a/" + "A" * 40,
        "a" * 39,
        "a" * 41,
        "a" * 63,
        "a" * 65,
        "/".join("a" for _ in range(MAX_BRANCH_COMPONENTS)),
    ],
)
def test_portable_name_grammar_accepts_only_deliberate_subset_members(branch: str) -> None:
    assert check_branch_name(policy("**"), branch).valid


@pytest.mark.parametrize(
    "branch",
    [
        "HEAD",
        "head",
        "refs/main",
        "Refs/heads/main",
        "a" * 40,
        "F" * 64,
        "1-first",
        "/feat",
        "feat/",
        "feat//topic",
        "feat/-topic",
        "feat/topic-",
        "feat/.topic",
        "feat/topic.",
        "feat/./topic",
        "feat/a..b",
        "feat/topic.lock",
        "feat/topic.LOCK",
        "feat/topic.lock.lock",
        "bad name",
        "bad\tname",
        "bad\x00name",
        "café",
        "feat/@topic",
        "/".join("a" for _ in range(MAX_BRANCH_COMPONENTS + 1)),
    ],
)
def test_representable_nonportable_names_report_only_syntax(branch: str) -> None:
    report = check_branch_name(policy("**"), branch)

    assert report.matched_pattern is None
    assert tuple(item.code for item in report.diagnostics) == (BRANCH_SYNTAX_CODE,)


def test_branch_admission_is_case_sensitive() -> None:
    report = check_branch_name(policy("Feat/**"), "feat/topic")

    assert tuple(item.code for item in report.diagnostics) == (BRANCH_ALLOWED_CODE,)


def test_checker_rejects_non_model_inputs() -> None:
    configured = policy("**")

    with pytest.raises(TypeError, match="BranchPolicy"):
        check_branch_name(cast(BranchPolicy, object()), "main")
    with pytest.raises(TypeError, match="string"):
        check_branch_name(configured, cast(str, object()))
