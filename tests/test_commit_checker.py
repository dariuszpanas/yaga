"""Tests for configurable Conventional Commit policy rules."""

from __future__ import annotations

from dataclasses import replace

import pytest

from yaga.commits.checker import check_header, check_target
from yaga.commits.models import (
    CasePolicy,
    CheckResult,
    CommitPolicy,
    CommitTarget,
    EndingPolicy,
    MergePolicy,
    PresencePolicy,
)
from yaga.commits.parser import MAX_MESSAGE_BYTES
from yaga.errors import InputError

DEFAULT_POLICY = CommitPolicy()


def result(message: str, policy: CommitPolicy | None = None) -> CheckResult:
    return check_target(CommitTarget(label="test", message=message), policy or DEFAULT_POLICY)


def codes(message: str, policy: CommitPolicy) -> list[str]:
    return [diagnostic.code for diagnostic in result(message, policy).diagnostics]


def test_default_policy_enforces_structure_without_an_opinionated_type_list() -> None:
    assert result("custom(Module): accept project vocabulary").valid
    assert codes("not conventional", CommitPolicy()) == ["syntax.header"]


@pytest.mark.parametrize(
    "message",
    [
        ": missing type",
        "fix(): empty scope",
        "fix( ): whitespace-only scope",
        "fix(api client): whitespace-bearing scope",
        "fi\u202ex: format control in type",
        "fix(api\u200b): format control in scope",
        "fi\x00x: control in type",
        "fix(api\x00): control in scope",
    ],
)
def test_default_policy_rejects_invalid_type_and_scope_tokens(message: str) -> None:
    assert codes(message, CommitPolicy()) == ["syntax.header"]


def test_type_case_and_allow_list_are_independent_rules() -> None:
    policy = CommitPolicy(allowed_types=("feat", "fix"), type_case=CasePolicy.LOWER)

    assert codes("FEAT: add a command", policy) == ["type.case"]
    assert codes("docs: explain the command", policy) == ["type.allowed"]
    assert result("straße: support Unicode types", CommitPolicy(type_case=CasePolicy.LOWER)).valid


def test_scope_presence_and_allow_list_are_configurable() -> None:
    required = CommitPolicy(
        scope_policy=PresencePolicy.REQUIRED,
        allowed_scopes=("cli", "git"),
        scope_case=CasePolicy.LOWER,
    )
    assert codes("feat: add a command", required) == ["scope.required"]
    assert codes("feat(API): add a command", required) == ["scope.case", "scope.allowed"]
    assert result("feat(cli): add a command", required).valid

    forbidden = replace(required, scope_policy=PresencePolicy.FORBIDDEN, allowed_scopes=None)
    assert codes("feat(cli): add a command", forbidden) == ["scope.forbidden"]


def test_header_and_description_bounds_are_reported_deterministically() -> None:
    policy = CommitPolicy(
        header_max_length=20,
        description_min_length=10,
        description_max_length=12,
    )

    assert codes("feat: short", policy) == ["description.length"]
    assert codes("feat: this description is much too long", policy) == [
        "header.length",
        "description.length",
    ]


def test_description_ending_policy_supports_require_and_forbid() -> None:
    assert codes(
        "fix: avoid punctuation.",
        CommitPolicy(description_ending=EndingPolicy.FORBID),
    ) == ["description.ending"]
    assert codes(
        "fix: require punctuation",
        CommitPolicy(description_ending=EndingPolicy.REQUIRE),
    ) == ["description.ending"]


def test_body_policy_does_not_count_footers_as_body() -> None:
    required = CommitPolicy(body_policy=PresencePolicy.REQUIRED, body_min_length=10)
    message = "feat!: replace the API\n\nBREAKING CHANGE: use the new command"

    assert codes(message, required) == ["body.required"]


def test_body_structure_length_and_line_bounds_are_independent() -> None:
    policy = CommitPolicy(body_min_length=20, body_max_line_length=12)
    message = "fix: preserve input\nshort body"

    assert codes(message, policy) == [
        "syntax.separator",
        "body.length",
    ]
    long_line = "fix: preserve input\n\nThis body line is long enough"
    assert codes(long_line, policy) == ["body.line-length"]


def test_merge_policy_uses_parent_identity_not_header_text() -> None:
    merge = CommitTarget(
        label="merge",
        message="Merge branch 'main'",
        parents=("a" * 40, "b" * 40),
    )

    assert check_target(merge, CommitPolicy()).status == "skipped"
    assert [
        diagnostic.code
        for diagnostic in check_target(
            merge, CommitPolicy(merge_commits=MergePolicy.REJECT)
        ).diagnostics
    ] == ["merge.rejected"]
    assert [
        diagnostic.code
        for diagnostic in check_target(
            merge, CommitPolicy(merge_commits=MergePolicy.CHECK)
        ).diagnostics
    ] == ["syntax.header"]


def test_ignored_headers_use_bounded_glob_patterns() -> None:
    policy = CommitPolicy(ignored_headers=('Revert "*"',))

    assert result('Revert "feat: temporary change"', policy).status == "skipped"


def test_hard_message_limit_applies_before_policy_rules() -> None:
    oversized = "feat: " + "x" * MAX_MESSAGE_BYTES

    assert codes(oversized, CommitPolicy()) == ["message.size"]


def test_domain_checker_rejects_non_utf8_python_strings() -> None:
    with pytest.raises(InputError, match="valid UTF-8"):
        result("feat: invalid \udcff")


def test_header_check_applies_header_rules_without_commit_body_policy() -> None:
    policy = CommitPolicy(
        allowed_types=("fix",),
        body_policy=PresencePolicy.REQUIRED,
        body_min_length=100,
        body_max_line_length=1,
    )

    valid = check_header(CommitTarget(label="title", message="fix: repair title"), policy)
    invalid = check_header(CommitTarget(label="title", message="feat: wrong type"), policy)

    assert valid.valid
    assert [diagnostic.code for diagnostic in invalid.diagnostics] == ["type.allowed"]
