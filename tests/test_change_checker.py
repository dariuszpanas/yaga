"""Tests for pure changed-path coupling evaluation and models."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from yaga.changes import checker as change_checker
from yaga.changes.checker import MAX_CHANGE_MATCH_WORK, check_changed_paths
from yaga.changes.models import (
    CHANGE_REQUIRE_ANY_CODE,
    MAX_CHANGE_PATTERNS,
    MAX_CHANGE_RULES,
    MAX_CHANGED_PATH_BYTES,
    MAX_CHANGED_PATH_COMPONENTS,
    MAX_CHANGED_PATHS,
    ChangePolicy,
    ChangeReport,
    ChangeRule,
    ChangeRuleResult,
    ChangeRuleStatus,
    ChangeSelection,
)
from yaga.errors import ConfigurationError

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def selection(*paths: str) -> ChangeSelection:
    return ChangeSelection(
        revision_range="main...HEAD",
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        comparison_sha=BASE_SHA,
        paths=tuple(paths),
    )


def rule(
    name: str,
    when_any: tuple[str, ...],
    require_any: tuple[str, ...],
) -> ChangeRule:
    return ChangeRule(name=name, when_any=when_any, require_any=require_any)


def test_checker_preserves_policy_order_and_reports_skipped_passed_and_failed_rules() -> None:
    policy = ChangePolicy(
        change_policy_version=1,
        rules=(
            rule("docs-needs-changelog", ("docs/**",), ("CHANGELOG.md",)),
            rule("python-source-needs-tests", ("src/**/*.py",), ("tests/**/*.py",)),
            rule("scripts-need-tests", ("scripts/**",), ("tests/**",)),
        ),
    )
    selected = selection("docs/readme.md", "src/yaga.py", "tests/test_yaga.py")

    report = check_changed_paths(policy, selected)

    assert report.selection is selected
    assert tuple(result.rule.name for result in report.results) == (
        "docs-needs-changelog",
        "python-source-needs-tests",
        "scripts-need-tests",
    )
    assert report.results[0].triggered_paths == ("docs/readme.md",)
    assert report.results[0].required_paths == ()
    assert report.results[0].status is ChangeRuleStatus.FAILED
    assert report.results[1].triggered_paths == ("src/yaga.py",)
    assert report.results[1].required_paths == ("tests/test_yaga.py",)
    assert report.results[1].status is ChangeRuleStatus.PASSED
    assert report.results[2].triggered_paths == ()
    assert report.results[2].required_paths == ()
    assert report.results[2].status is ChangeRuleStatus.SKIPPED
    assert (report.checked, report.skipped, report.passed, report.failed) == (3, 1, 1, 1)
    assert not report.valid


def test_untriggered_rule_does_not_carry_unrelated_required_matches() -> None:
    policy = ChangePolicy(
        1,
        (rule("python-source-needs-tests", ("src/**",), ("tests/**",)),),
    )

    result = check_changed_paths(policy, selection("tests/test_yaga.py")).results[0]

    assert result.status is ChangeRuleStatus.SKIPPED
    assert result.triggered_paths == ()
    assert result.required_paths == ()
    assert result.valid


def test_when_any_and_require_any_are_existential_and_path_matches_are_deduplicated() -> None:
    policy = ChangePolicy(
        1,
        (
            rule(
                "source-needs-tests",
                ("src/**", "**/*.py"),
                ("tests/**", "**/test_*.py"),
            ),
        ),
    )
    selected = selection("docs/guide.md", "src/a.py", "src/b.py", "tests/test_a.py")

    result = check_changed_paths(policy, selected).results[0]

    assert result.triggered_paths == ("src/a.py", "src/b.py", "tests/test_a.py")
    assert result.required_paths == ("tests/test_a.py",)
    assert result.status is ChangeRuleStatus.PASSED


def test_literal_dollar_percent_and_case_sensitive_matching_survive_evaluation() -> None:
    policy = ChangePolicy(
        1,
        (
            rule(
                "generated-needs-tests",
                ("$SRC/%generated%/**",),
                ("$TESTS/%generated%/**",),
            ),
        ),
    )

    failed = check_changed_paths(policy, selection("$SRC/%generated%/module.py"))
    passed = check_changed_paths(
        policy,
        selection("$SRC/%generated%/module.py", "$TESTS/%generated%/test_module.py"),
    )

    assert failed.failed == 1
    assert passed.passed == 1
    assert (
        check_changed_paths(
            policy,
            selection("$src/%generated%/module.py"),
        ).skipped
        == 1
    )


def test_empty_change_selection_skips_every_rule_and_is_valid() -> None:
    policy = ChangePolicy(
        1,
        (rule("python-source-needs-tests", ("src/**",), ("tests/**",)),),
    )

    report = check_changed_paths(policy, selection())

    assert report.checked == 1
    assert report.skipped == 1
    assert report.passed == 0
    assert report.failed == 0
    assert report.valid


def test_result_exposes_the_stable_violation_code() -> None:
    configured_rule = rule("python-source-needs-tests", ("src/**",), ("tests/**",))
    result = ChangeRuleResult(configured_rule, ("src/yaga.py",), ())

    assert result.code == CHANGE_REQUIRE_ANY_CODE == "change.require_any"
    assert result.status is ChangeRuleStatus.FAILED
    assert not result.valid


@pytest.mark.parametrize(
    "change_selection",
    [
        lambda: ChangeSelection("main branch...HEAD", BASE_SHA, HEAD_SHA, BASE_SHA, ()),
        lambda: ChangeSelection("main\n...HEAD", BASE_SHA, HEAD_SHA, BASE_SHA, ()),
        lambda: ChangeSelection("main...HEAD", BASE_SHA.upper(), HEAD_SHA, BASE_SHA, ()),
        lambda: ChangeSelection("main...HEAD", BASE_SHA, "b" * 64, BASE_SHA, ()),
        lambda: ChangeSelection(
            "main...HEAD",
            BASE_SHA,
            HEAD_SHA,
            BASE_SHA,
            ("tests/test_a.py", "src/a.py"),
        ),
        lambda: ChangeSelection(
            "main...HEAD",
            BASE_SHA,
            HEAD_SHA,
            BASE_SHA,
            ("src/a.py", "src/a.py"),
        ),
        lambda: ChangeSelection("main...HEAD", BASE_SHA, HEAD_SHA, BASE_SHA, ("../src/a.py",)),
        lambda: ChangeSelection("main...HEAD", BASE_SHA, HEAD_SHA, BASE_SHA, (".",)),
        lambda: ChangeSelection(
            "main...HEAD",
            BASE_SHA,
            HEAD_SHA,
            BASE_SHA,
            ("/".join("a" for _ in range(MAX_CHANGED_PATH_COMPONENTS + 1)),),
        ),
        lambda: ChangeSelection(
            "main...HEAD",
            BASE_SHA,
            HEAD_SHA,
            BASE_SHA,
            ("é" * (MAX_CHANGED_PATH_BYTES // 2 + 1),),
        ),
        lambda: ChangeSelection(
            "main...HEAD",
            BASE_SHA,
            HEAD_SHA,
            BASE_SHA,
            tuple(f"file-{index:04}.txt" for index in range(MAX_CHANGED_PATHS + 1)),
        ),
    ],
)
def test_change_selection_requires_normalized_bounded_state(
    change_selection: Callable[[], ChangeSelection],
) -> None:
    with pytest.raises(ValueError):
        change_selection()


def test_change_selection_accepts_exact_path_count_and_component_limits() -> None:
    paths = tuple(f"file-{index:04}.txt" for index in range(MAX_CHANGED_PATHS))
    deepest = "/".join("a" for _ in range(MAX_CHANGED_PATH_COMPONENTS))

    assert len(selection(*paths).paths) == MAX_CHANGED_PATHS
    assert selection(deepest).paths == (deepest,)


def test_result_and_report_models_reject_noncanonical_or_unrelated_matches() -> None:
    configured_rule = rule("python-source-needs-tests", ("src/**",), ("tests/**",))
    selected = selection("src/a.py", "tests/test_a.py")

    with pytest.raises(ValueError, match="skipped"):
        ChangeRuleResult(configured_rule, (), ("tests/test_a.py",))
    with pytest.raises(ValueError, match="unique and sorted"):
        ChangeRuleResult(configured_rule, ("tests/test_a.py", "src/a.py"), ())
    with pytest.raises(ValueError, match="belong to its selection"):
        ChangeReport(
            selected,
            (ChangeRuleResult(configured_rule, ("src/missing.py",), ()),),
        )


def test_checker_rejects_non_model_inputs() -> None:
    policy = ChangePolicy(
        1,
        (rule("python-source-needs-tests", ("src/**",), ("tests/**",)),),
    )
    selected = selection("src/a.py")

    with pytest.raises(TypeError, match="ChangePolicy"):
        check_changed_paths(cast(ChangePolicy, object()), selected)
    with pytest.raises(TypeError, match="ChangeSelection"):
        check_changed_paths(policy, cast(ChangeSelection, object()))


def test_checker_compiles_and_matches_repeated_patterns_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ChangePolicy(
        1,
        tuple(
            rule(f"rule-{index}", ("src/**",), ("tests/**",)) for index in range(MAX_CHANGE_RULES)
        ),
    )
    original = change_checker._matching_paths
    matched_patterns = 0

    def counted_matching_paths(*args: object, **kwargs: object) -> tuple[str, ...]:
        nonlocal matched_patterns
        matched_patterns += 1
        function = cast(Callable[..., tuple[str, ...]], original)
        return function(*args, **kwargs)

    monkeypatch.setattr(change_checker, "_matching_paths", counted_matching_paths)

    report = check_changed_paths(policy, selection("src/a.py", "tests/test_a.py"))

    assert report.passed == MAX_CHANGE_RULES
    assert matched_patterns == 2


def test_checker_allows_many_distinct_shallow_patterns_within_match_work_budget() -> None:
    policy = ChangePolicy(
        1,
        tuple(
            rule(
                f"rule-{rule_index}",
                tuple(
                    f"src/pkg-{rule_index:02}-{pattern_index:02}/*.py"
                    for pattern_index in range(MAX_CHANGE_PATTERNS)
                ),
                ("tests/**",),
            )
            for rule_index in range(MAX_CHANGE_RULES)
        ),
    )
    paths = tuple(f"src/changed-{index:04}.py" for index in range(1_024))

    report = check_changed_paths(policy, selection(*paths))

    assert report.skipped == MAX_CHANGE_RULES
    assert MAX_CHANGE_MATCH_WORK == 10_000_000


def test_checker_match_work_limit_is_inclusive_and_routes_exhaustion_as_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep_pattern = "/".join("**" for _ in range(MAX_CHANGED_PATH_COMPONENTS))
    deep_path = "/".join("a" for _ in range(MAX_CHANGED_PATH_COMPONENTS))
    policy = ChangePolicy(1, (rule("deep-path", (deep_pattern,), (deep_pattern,)),))
    exact_work = MAX_CHANGED_PATH_COMPONENTS**2

    monkeypatch.setattr(change_checker, "MAX_CHANGE_MATCH_WORK", exact_work)
    assert check_changed_paths(policy, selection(deep_path)).passed == 1

    monkeypatch.setattr(change_checker, "MAX_CHANGE_MATCH_WORK", exact_work - 1)
    with pytest.raises(ConfigurationError, match="match-work limit"):
        check_changed_paths(policy, selection(deep_path))
