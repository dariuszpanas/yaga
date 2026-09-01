"""Tests for configurable Conventional Commit policy rules."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

import yaga.commits.checker as commit_checker
from yaga.commits.checker import check_header, check_target
from yaga.commits.models import (
    BreakingMarkerPolicy,
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


@pytest.mark.parametrize(
    ("message", "valid"),
    [
        ("feat: preserve compatibility", True),
        ("feat!: remove the old command", False),
        (
            "feat: remove the old command\n\nBREAKING CHANGE: use the replacement",
            False,
        ),
        (
            "feat!: remove the old command\n\nBREAKING CHANGE: use the replacement",
            True,
        ),
    ],
)
def test_paired_breaking_markers_use_an_exact_truth_table(message: str, valid: bool) -> None:
    checked = result(
        message,
        CommitPolicy(breaking_markers=BreakingMarkerPolicy.PAIRED),
    )

    assert checked.valid is valid
    assert [diagnostic.code for diagnostic in checked.diagnostics] == (
        [] if valid else ["breaking.marker-pair"]
    )


def test_breaking_marker_pair_diagnostic_is_stable_and_ordered_before_body_rules() -> None:
    checked = result(
        "feat: remove the old command\n\nBREAKING CHANGE: use the replacement",
        CommitPolicy(
            body_policy=PresencePolicy.REQUIRED,
            breaking_markers=BreakingMarkerPolicy.PAIRED,
        ),
    )

    assert [diagnostic.code for diagnostic in checked.diagnostics] == [
        "breaking.marker-pair",
        "body.required",
    ]
    assert checked.diagnostics[0].message == (
        "breaking changes must use both ! and a BREAKING CHANGE footer"
    )
    assert checked.diagnostics[0].line == 1


def test_paired_breaking_markers_accept_the_hyphenated_footer_synonym() -> None:
    policy = CommitPolicy(breaking_markers=BreakingMarkerPolicy.PAIRED)

    assert result(
        "feat!: remove the old command\n\nBREAKING-CHANGE: use the replacement",
        policy,
    ).valid


def test_paired_breaking_footer_continuation_is_excluded_from_body_policy() -> None:
    policy = CommitPolicy(
        body_min_words=100,
        breaking_markers=BreakingMarkerPolicy.PAIRED,
    )

    assert result(
        "feat!: remove the old command\n\n"
        "BREAKING CHANGE: use the replacement\n\n"
        "Migrate callers before upgrading to the next release.",
        policy,
    ).valid


def test_lowercase_breaking_footer_does_not_complete_the_marker_pair() -> None:
    policy = CommitPolicy(breaking_markers=BreakingMarkerPolicy.PAIRED)

    assert codes(
        "feat!: remove the old command\n\nbreaking change: use the replacement",
        policy,
    ) == ["breaking.marker-pair"]


def test_either_breaking_marker_policy_preserves_spec_permitted_forms() -> None:
    policy = CommitPolicy(breaking_markers=BreakingMarkerPolicy.EITHER)

    assert result("feat!: remove the old command", policy).valid
    assert result(
        "feat: remove the old command\n\nBREAKING CHANGE: use the replacement",
        policy,
    ).valid


def test_body_policy_does_not_count_footers_as_body() -> None:
    required = CommitPolicy(
        body_policy=PresencePolicy.REQUIRED,
        body_min_length=10,
        body_min_words=10,
    )
    message = "feat!: replace the API\n\nBREAKING CHANGE: use the new command"

    assert codes(message, required) == ["body.required"]


def test_body_min_words_counts_only_tokens_with_unicode_alphanumeric_characters() -> None:
    policy = CommitPolicy(body_min_words=5)
    message = (
        "feat: describe token counting\n\nnaïve\t１２３\u2003e\u0301lan ... -- 👾 \u0301\u0301 東京"
    )

    checked = result(message, policy)

    assert [diagnostic.code for diagnostic in checked.diagnostics] == ["body.word-count"]
    assert checked.diagnostics[0].message == "body has 4 words; minimum is 5"
    assert result(message, replace(policy, body_min_words=4)).valid


def test_body_min_words_reports_the_body_start_line_at_the_lower_boundary() -> None:
    policy = CommitPolicy(body_min_words=3)
    message = "fix: document behavior\n\none two ..."

    checked = result(message, policy)

    assert [diagnostic.code for diagnostic in checked.diagnostics] == ["body.word-count"]
    assert checked.diagnostics[0].message == "body has 2 words; minimum is 3"
    assert checked.diagnostics[0].line == 3
    assert result("fix: document behavior\n\none two three", policy).valid


def test_body_min_words_uses_singular_diagnostic_grammar() -> None:
    checked = result(
        "docs: explain a short body\n\none",
        CommitPolicy(body_min_words=2),
    )

    assert checked.diagnostics[0].message == "body has 1 word; minimum is 2"


def test_body_min_words_excludes_the_recognized_final_footer_block() -> None:
    policy = CommitPolicy(body_min_words=3)
    message = "feat: retain footer parsing\n\none two\n\nRefs: issue 123\ncontinued footer value"

    checked = result(message, policy)

    assert [diagnostic.code for diagnostic in checked.diagnostics] == ["body.word-count"]
    assert checked.diagnostics[0].message == "body has 2 words; minimum is 3"


def test_body_min_words_is_not_applied_when_no_body_exists() -> None:
    policy = CommitPolicy(body_min_words=100)

    assert result("feat: allow an absent body", policy).valid
    assert result("feat: allow footers\n\nRefs: issue 123", policy).valid


def test_disabled_body_min_words_does_not_call_the_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_counter(_value: str, _minimum: int) -> int | None:
        raise AssertionError("disabled body word policy invoked the counter")

    monkeypatch.setattr(commit_checker, "_word_count_below_minimum", unexpected_counter)

    assert result("feat: keep the default fast\n\nbody words are present").valid


@pytest.mark.parametrize(
    ("body", "minimum", "below_minimum"),
    [
        ("... -- 👾 \u0301\u0301", 1, 0),
        ("one-two", 2, 1),
        ("one\u2003two", 3, 2),
        ("one ... two", 2, None),
    ],
)
def test_body_word_counter_reports_only_below_minimum_counts(
    body: str,
    minimum: int,
    below_minimum: int | None,
) -> None:
    assert commit_checker._word_count_below_minimum(body, minimum) == below_minimum


def test_body_word_counter_stops_as_soon_as_the_minimum_is_reached() -> None:
    class ExplodingTail(str):
        def __iter__(self) -> Iterator[str]:
            yield "o"
            raise AssertionError("counter scanned beyond the satisfied minimum")

    assert commit_checker._word_count_below_minimum(ExplodingTail("opaque"), 1) is None


def test_body_structure_length_and_line_bounds_are_independent() -> None:
    policy = CommitPolicy(body_min_length=20, body_max_line_length=12)
    message = "fix: preserve input\nshort body"

    assert codes(message, policy) == [
        "syntax.separator",
        "body.length",
    ]
    long_line = "fix: preserve input\n\nThis body line is long enough"
    assert codes(long_line, policy) == ["body.line-length"]


def test_body_diagnostics_have_stable_length_word_and_line_order() -> None:
    policy = CommitPolicy(
        body_min_length=20,
        body_min_words=4,
        body_max_line_length=5,
    )

    assert codes("fix: preserve input\n\none two", policy) == [
        "body.length",
        "body.word-count",
        "body.line-length",
    ]


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
        body_min_words=100,
        body_max_line_length=1,
        breaking_markers=BreakingMarkerPolicy.PAIRED,
    )

    valid = check_header(CommitTarget(label="title", message="fix!: repair title"), policy)
    invalid = check_header(CommitTarget(label="title", message="feat: wrong type"), policy)

    assert valid.valid
    assert [diagnostic.code for diagnostic in invalid.diagnostics] == ["type.allowed"]
