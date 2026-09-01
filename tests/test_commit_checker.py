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


def test_scope_policy_by_type_overrides_global_presence_policy() -> None:
    policy = CommitPolicy(
        scope_policy=PresencePolicy.OPTIONAL,
        scope_policy_by_type=(
            ("feat", PresencePolicy.REQUIRED),
            ("docs", PresencePolicy.FORBIDDEN),
        ),
    )

    assert codes("feat: add a command", policy) == ["scope.required"]
    assert result("feat(cli): add a command", policy).valid
    assert codes("docs(guide): explain the command", policy) == ["scope.forbidden"]
    assert result("docs: explain the command", policy).valid

    optional_override = CommitPolicy(
        scope_policy=PresencePolicy.REQUIRED,
        scope_policy_by_type=(("chore", PresencePolicy.OPTIONAL),),
    )
    assert result("chore: refresh tooling", optional_override).valid
    assert result("chore(deps): refresh tooling", optional_override).valid


def test_scope_policy_by_type_falls_back_and_matches_type_case_insensitively() -> None:
    policy = CommitPolicy(
        scope_policy=PresencePolicy.REQUIRED,
        scope_policy_by_type=(("FEAT", PresencePolicy.OPTIONAL),),
    )

    assert result("feat: add a command", policy).valid
    assert result("FeAt: add a command", policy).valid
    assert codes("fix: repair the command", policy) == ["scope.required"]


def test_scope_override_does_not_replace_scope_case_or_allow_list_rules() -> None:
    policy = CommitPolicy(
        scope_policy_by_type=(("docs", PresencePolicy.FORBIDDEN),),
        allowed_scopes=("guide",),
        scope_case=CasePolicy.LOWER,
    )

    assert codes("docs(API): explain the command", policy) == [
        "scope.forbidden",
        "scope.case",
        "scope.allowed",
    ]


def test_header_check_applies_scope_policy_by_type() -> None:
    policy = CommitPolicy(
        scope_policy=PresencePolicy.OPTIONAL,
        scope_policy_by_type=(
            ("feat", PresencePolicy.REQUIRED),
            ("docs", PresencePolicy.FORBIDDEN),
        ),
    )

    missing = check_header(CommitTarget(label="title", message="FEAT: add command"), policy)
    forbidden = check_header(
        CommitTarget(label="title", message="docs(guide): explain command"),
        policy,
    )

    assert [diagnostic.code for diagnostic in missing.diagnostics] == ["scope.required"]
    assert [diagnostic.code for diagnostic in forbidden.diagnostics] == ["scope.forbidden"]


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


def test_required_footer_tokens_match_exact_tokens_case_insensitively() -> None:
    policy = CommitPolicy(required_footer_tokens=("Signed-off-by", "Refs"))
    message = (
        "feat: enforce repository footers\n\nBody remains independent.\n\n"
        "signed-OFF-by: Example Maintainer <maintainer@example.com>\n"
        "Refs #123"
    )

    assert result(message, policy).valid


def test_required_footer_token_like_body_line_remains_missing() -> None:
    policy = CommitPolicy(required_footer_tokens=("Refs",))
    message = "feat: retain body prose\n\nExplain the issue.\nRefs #123"

    assert codes(message, policy) == ["footer.required"]


def test_required_footer_diagnostic_is_bounded_and_uses_configuration_order() -> None:
    policy = CommitPolicy(required_footer_tokens=("Signed-off-by", "Refs", "Co-authored-by"))

    checked = result("feat: require project metadata", policy)

    assert [diagnostic.code for diagnostic in checked.diagnostics] == ["footer.required"]
    assert checked.diagnostics[0].message == (
        "required footer token 'Signed-off-by' is missing; "
        "2 additional required footer tokens missing"
    )
    assert checked.diagnostics[0].line == 1


def test_forbidden_footer_reports_the_earliest_exact_occurrence() -> None:
    policy = CommitPolicy(forbidden_footer_tokens=("WIP", "Reviewed-by"))
    message = (
        "fix: reject temporary metadata\n\n"
        "reviewed-BY: Example Maintainer\n"
        "WIP #remove-before-merge"
    )

    checked = result(message, policy)

    assert [diagnostic.code for diagnostic in checked.diagnostics] == ["footer.forbidden"]
    assert checked.diagnostics[0].message == "footer token 'Reviewed-by' is forbidden by policy"
    assert checked.diagnostics[0].line == 3


def test_missing_header_separator_precedes_a_source_located_footer_finding() -> None:
    checked = result(
        "fix: retain structural findings\nWIP: remove before merge",
        CommitPolicy(forbidden_footer_tokens=("WIP",)),
    )

    assert [diagnostic.code for diagnostic in checked.diagnostics] == [
        "syntax.separator",
        "footer.forbidden",
    ]
    assert checked.diagnostics[1].line == 2


def test_crlf_and_leading_blank_lines_preserve_footer_location() -> None:
    checked = result(
        "fix: normalize source locations\r\n\r\n\r\nWIP: remove before merge",
        CommitPolicy(forbidden_footer_tokens=("WIP",)),
    )

    assert [diagnostic.code for diagnostic in checked.diagnostics] == ["footer.forbidden"]
    assert checked.diagnostics[0].line == 4


def test_footer_policies_do_not_match_prefixes_values_or_non_footer_body_lines() -> None:
    policy = CommitPolicy(forbidden_footer_tokens=("WIP",))

    assert result("fix: keep exact matching\n\nWIP-Mode: allowed", policy).valid
    assert result("fix: keep exact matching\n\nRefs: WIP", policy).valid
    assert result("fix: keep body text\n\nBody paragraph\nWIP: still body text", policy).valid


def test_footer_diagnostics_follow_existing_body_diagnostics_in_stable_order() -> None:
    policy = CommitPolicy(
        body_min_words=3,
        required_footer_tokens=("Signed-off-by",),
        forbidden_footer_tokens=("WIP",),
    )
    message = "fix: order policy findings\n\none two\n\nWIP: temporary"

    checked = result(message, policy)

    assert [diagnostic.code for diagnostic in checked.diagnostics] == [
        "body.word-count",
        "footer.required",
        "footer.forbidden",
    ]
    assert checked.diagnostics[-1].line == 5


def test_disabled_footer_policies_do_not_scan_footer_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_scan(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("disabled footer policy invoked the footer iterator")

    monkeypatch.setattr(commit_checker, "iter_footer_starts", unexpected_scan)

    assert result("fix: keep defaults fast\n\nRefs #42").valid


@pytest.mark.parametrize(
    ("count", "suffix"),
    [
        (2, "1 additional required footer token missing"),
        (128, "127 additional required footer tokens missing"),
    ],
)
def test_required_footer_diagnostic_remains_single_and_bounded(
    count: int,
    suffix: str,
) -> None:
    policy = CommitPolicy(required_footer_tokens=tuple(f"Token-{index}" for index in range(count)))

    checked = result("feat: bound missing footer reports", policy)

    assert len(checked.diagnostics) == 1
    assert checked.diagnostics[0].code == "footer.required"
    assert checked.diagnostics[0].message == f"required footer token 'Token-0' is missing; {suffix}"


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
        required_footer_tokens=("Signed-off-by",),
        forbidden_footer_tokens=("WIP",),
    )

    valid = check_header(CommitTarget(label="title", message="fix!: repair title"), policy)
    invalid = check_header(CommitTarget(label="title", message="feat: wrong type"), policy)

    assert valid.valid
    assert [diagnostic.code for diagnostic in invalid.diagnostics] == ["type.allowed"]
