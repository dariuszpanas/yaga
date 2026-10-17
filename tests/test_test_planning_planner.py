"""Selection behavior, determinism, and fail-closed resource boundaries."""

from __future__ import annotations

import json
import subprocess
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from typing import cast

import pytest

from yaga.errors import ConfigurationError, InputError
from yaga.test_planning import planner
from yaga.test_planning.models import (
    Profile,
    QualificationPolicy,
    SelectionMode,
    SelectionRule,
    Suite,
    UnknownPathPolicy,
)
from yaga.test_planning.planner import plan_tests
from yaga.test_planning.selection import SelectionPlan, SelectionReason


@pytest.fixture
def policy() -> QualificationPolicy:
    return QualificationPolicy(
        1,
        suites=(
            Suite("smoke", "smoke"),
            Suite("build", "image", variants=("arm", "amd"), requires=("docker",)),
            Suite("backend", "pytest", tags=("api",), needs=("build",)),
            Suite("frontend", "vitest", tags=("api",)),
        ),
        profiles=(
            Profile("development", SelectionMode.SELECTIVE, always=("smoke",)),
            Profile("empty", SelectionMode.SELECTIVE),
            Profile("release", SelectionMode.FULL),
        ),
        rules=(
            SelectionRule("backend", ("src/**",), select=("backend",)),
            SelectionRule("shared", ("schema/**",), select_tags=("api",)),
        ),
        full_on=("lockfile",),
        no_impact=("docs/**",),
    )


def ids(plan: SelectionPlan) -> tuple[str, ...]:
    return tuple(item.suite for item in plan.selected)


def test_additive_rules_tags_and_prerequisites_select_every_variant(
    policy: QualificationPolicy,
) -> None:
    plan = plan_tests(policy, profile="development", changed_paths=("schema/api", "src/app"))
    assert ids(plan) == ("build", "backend", "frontend", "smoke")
    assert plan.selected[0].variants == ("amd", "arm")
    assert next(suite for suite in plan.inventory if suite.id == "build").requires == ("docker",)
    assert plan.reasons == (
        SelectionReason("test.always", suite="smoke"),
        SelectionReason("test.prerequisite", suite="build", required_by="backend"),
        SelectionReason("test.rule", suite="backend", rule="backend", path="src/app"),
        SelectionReason("test.rule", suite="backend", rule="shared", path="schema/api"),
        SelectionReason("test.rule", suite="frontend", rule="shared", path="schema/api"),
    )


def test_no_impact_never_subtracts_rule_or_mandatory_selection(policy: QualificationPolicy) -> None:
    policy = replace(policy, no_impact=("**",))
    plan = plan_tests(policy, profile="development", changed_paths=("src/app", "docs/readme"))
    assert ids(plan) == ("build", "backend", "smoke")
    assert SelectionReason("test.no-impact", path="docs/readme") in plan.reasons
    assert SelectionReason("test.no-impact", path="src/app") not in plan.reasons


@pytest.mark.parametrize(("profile", "expected"), [("development", ("smoke",)), ("empty", ())])
def test_complete_empty_change_retains_only_mandatory_suites(
    policy: QualificationPolicy, profile: str, expected: tuple[str, ...]
) -> None:
    plan = plan_tests(policy, profile=profile, changed_paths=())
    assert ids(plan) == expected
    assert plan.changed_paths == ()
    assert plan.effective_mode is SelectionMode.SELECTIVE
    assert len(plan.inventory) == 4


def test_selective_profile_cannot_infer_missing_paths(policy: QualificationPolicy) -> None:
    with pytest.raises(InputError, match="requires explicit changed paths"):
        plan_tests(policy, profile="development", changed_paths=None)


@pytest.mark.parametrize("paths", [None, (), ("unmapped",)])
def test_full_profile_never_evaluates_impact_rules(
    policy: QualificationPolicy, paths: tuple[str, ...] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = replace(policy, unknown_paths=UnknownPathPolicy.ERROR)

    def unexpected_compile(*args: object, **kwargs: object) -> None:
        pytest.fail("full profile must not classify paths")

    monkeypatch.setattr(planner, "compile_pattern", unexpected_compile)
    plan = plan_tests(policy, profile="release", changed_paths=paths)
    assert set(ids(plan)) == {suite.id for suite in policy.suites}
    assert plan.requested_mode is plan.effective_mode is SelectionMode.FULL
    assert plan.reasons == (SelectionReason("test.profile-full"),)
    assert plan.changed_paths == paths


def test_full_trigger_precedes_unknown_error_for_the_complete_change(
    policy: QualificationPolicy,
) -> None:
    policy = replace(policy, unknown_paths=UnknownPathPolicy.ERROR, no_impact=("lockfile",))
    plan = plan_tests(policy, profile="development", changed_paths=("unknown", "lockfile"))
    assert set(ids(plan)) == {suite.id for suite in policy.suites}
    assert plan.requested_mode is SelectionMode.SELECTIVE
    assert plan.effective_mode is SelectionMode.FULL
    assert plan.reasons == (
        SelectionReason("test.always", suite="smoke"),
        SelectionReason("test.full-trigger", path="lockfile"),
    )


def test_unknown_path_falls_back_to_complete_inventory(policy: QualificationPolicy) -> None:
    plan = plan_tests(policy, profile="development", changed_paths=("src/app", "other"))
    assert plan.effective_mode is SelectionMode.FULL
    assert set(ids(plan)) == {suite.id for suite in policy.suites}
    assert SelectionReason("test.unknown-path", path="other") in plan.reasons
    assert SelectionReason("test.rule", suite="backend", rule="backend", path="src/app") in (
        plan.reasons
    )


def test_unknown_path_error_has_bounded_deterministic_detail(policy: QualificationPolicy) -> None:
    policy = replace(policy, unknown_paths=UnknownPathPolicy.ERROR)
    with pytest.raises(ConfigurationError, match="2 unknown changed path") as raised:
        plan_tests(policy, profile="development", changed_paths=("z", "a" * 4096))
    assert len(str(raised.value)) < 300
    assert "first: 'aaa" in str(raised.value)


@pytest.mark.parametrize(
    "path",
    [
        "/abs",
        "../x",
        "a//b",
        "a\\b",
        "a/",
        "\x1b[31m",
        "a\u202eb",
        "\ud800",
        pytest.param("a" * 4097, id="oversized"),
        3,
    ],
)
@pytest.mark.parametrize("profile", ["development", "release"])
def test_invalid_paths_never_become_full_or_empty_plans(
    policy: QualificationPolicy, path: object, profile: str
) -> None:
    with pytest.raises(InputError, match="invalid changed path"):
        plan_tests(policy, profile=profile, changed_paths=("lockfile", cast(str, path)))


@pytest.mark.parametrize("paths", [["src/app"], "src/app", {"src/app"}, iter(["src/app"])])
def test_only_explicit_immutable_path_collection_is_accepted(
    policy: QualificationPolicy, paths: object
) -> None:
    with pytest.raises(InputError, match="explicit tuple"):
        plan_tests(policy, profile="release", changed_paths=cast(tuple[str, ...], paths))


def test_duplicate_paths_are_rejected(policy: QualificationPolicy) -> None:
    with pytest.raises(InputError, match="unique"):
        plan_tests(policy, profile="release", changed_paths=("src/app", "src/app"))


@pytest.mark.parametrize(
    "profile",
    [None, 3, "", pytest.param("x" * 65, id="long"), pytest.param("x" * 100_000, id="huge")],
)
def test_profile_input_is_bounded_before_lookup(
    policy: QualificationPolicy, profile: object
) -> None:
    with pytest.raises(ConfigurationError, match="explicit string") as raised:
        plan_tests(policy, profile=cast(str, profile), changed_paths=())
    assert len(str(raised.value)) < 150


def test_unknown_profile_error_is_sanitized(policy: QualificationPolicy) -> None:
    with pytest.raises(ConfigurationError, match="unknown test profile") as raised:
        plan_tests(policy, profile="\x1b[31m\u202e", changed_paths=())
    assert "\x1b" not in str(raised.value)
    assert "\u202e" not in str(raised.value)


def test_path_count_boundary_is_inclusive(policy: QualificationPolicy) -> None:
    paths = tuple(f"file-{index}" for index in range(2048))
    assert (
        len(plan_tests(policy, profile="release", changed_paths=paths).changed_paths or ()) == 2048
    )
    with pytest.raises(InputError, match="at most 2048"):
        plan_tests(policy, profile="release", changed_paths=(*paths, "one-more"))


def test_aggregate_path_budget_counts_utf8_bytes_and_delimiters(
    policy: QualificationPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert planner.MAX_CHANGED_PATH_BYTES_TOTAL == 4 * 1024 * 1024
    monkeypatch.setattr(planner, "MAX_CHANGED_PATH_BYTES_TOTAL", 7)
    paths = ("é", "猫")  # 2 + 1 delimiter + 3 + 1 delimiter.
    assert plan_tests(policy, profile="release", changed_paths=paths).changed_paths == paths
    monkeypatch.setattr(planner, "MAX_CHANGED_PATH_BYTES_TOTAL", 6)
    with pytest.raises(InputError, match="6-byte limit"):
        plan_tests(policy, profile="release", changed_paths=paths)


def test_dependency_closure_records_shared_edges_and_uses_ready_id_order() -> None:
    policy = QualificationPolicy(
        1,
        suites=(
            Suite("a.consumer", "run", needs=("z.base",)),
            Suite("b.consumer", "run", needs=("z.base",)),
            Suite("m.free", "run"),
            Suite("z.base", "run", needs=("z.root",)),
            Suite("z.root", "run"),
        ),
        profiles=(Profile("dev", SelectionMode.SELECTIVE, ("b.consumer", "a.consumer", "m.free")),),
    )
    plan = plan_tests(policy, profile="dev", changed_paths=())
    assert ids(plan) == ("m.free", "z.root", "z.base", "a.consumer", "b.consumer")
    assert tuple(reason for reason in plan.reasons if reason.code == "test.prerequisite") == (
        SelectionReason("test.prerequisite", suite="z.base", required_by="a.consumer"),
        SelectionReason("test.prerequisite", suite="z.base", required_by="b.consumer"),
        SelectionReason("test.prerequisite", suite="z.root", required_by="z.base"),
    )


def test_equivalent_policy_order_and_paths_produce_identical_selection(
    policy: QualificationPolicy,
) -> None:
    other = replace(
        policy,
        suites=tuple(
            replace(suite, variants=tuple(reversed(suite.variants)))
            for suite in reversed(policy.suites)
        ),
        profiles=tuple(reversed(policy.profiles)),
        rules=tuple(reversed(policy.rules)),
    )
    paths = ("schema/api", "src/app", "docs/readme")
    assert plan_tests(policy, profile="development", changed_paths=paths) == plan_tests(
        other, profile="development", changed_paths=tuple(reversed(paths))
    )


def test_selection_and_nested_catalog_are_immutable(policy: QualificationPolicy) -> None:
    plan = plan_tests(policy, profile="release", changed_paths=None)
    with pytest.raises(FrozenInstanceError):
        plan.profile = "other"  # ty: ignore[invalid-assignment]
    with pytest.raises(FrozenInstanceError):
        plan.inventory[0].variants = ()  # ty: ignore[invalid-assignment]
    with pytest.raises(FrozenInstanceError):
        plan.selected[0].variants = ()  # ty: ignore[invalid-assignment]


def test_planner_does_not_read_files_or_spawn_commands(
    policy: QualificationPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_io(*args: object, **kwargs: object) -> None:
        pytest.fail("pure planner performed I/O")

    with monkeypatch.context() as context:
        context.setattr(Path, "open", unexpected_io)
        context.setattr(Path, "read_bytes", unexpected_io)
        context.setattr(subprocess, "run", unexpected_io)
        context.setattr(subprocess, "Popen", unexpected_io)
        plan = plan_tests(policy, profile="development", changed_paths=("src/app",))
    assert ids(plan) == ("build", "backend", "smoke")


def test_each_unique_pattern_is_compiled_and_evaluated_once(
    policy: QualificationPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled: list[str] = []
    original = planner.compile_pattern

    def tracked_compile(pattern: str):
        compiled.append(pattern)
        return original(pattern)

    monkeypatch.setattr(planner, "compile_pattern", tracked_compile)
    policy = replace(
        policy,
        full_on=("unused",),
        no_impact=("unused", "src/**", "docs/**"),
        rules=(
            SelectionRule("one", ("src/**",), select=("backend",)),
            SelectionRule("two", ("src/**", "unused"), select=("frontend",)),
        ),
    )
    plan = plan_tests(policy, profile="development", changed_paths=("src/app", "docs/readme"))
    assert len(compiled) == len(set(compiled)) == 3
    assert len([reason for reason in plan.reasons if reason.code == "test.rule"]) == 2


def test_match_work_is_shared_across_full_rule_and_no_impact_patterns(
    policy: QualificationPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert planner.MAX_MATCH_WORK == 10_000_000
    policy = replace(
        policy,
        full_on=("z",),
        rules=(SelectionRule("a", ("a",), select=("backend",)),),
        no_impact=("b",),
    )
    monkeypatch.setattr(planner, "MAX_MATCH_WORK", 18)
    assert plan_tests(policy, profile="empty", changed_paths=("a", "b")).effective_mode is (
        SelectionMode.SELECTIVE
    )
    monkeypatch.setattr(planner, "MAX_MATCH_WORK", 17)
    with pytest.raises(ConfigurationError, match="17-unit match-work limit"):
        plan_tests(policy, profile="empty", changed_paths=("a", "b"))


def test_semantic_output_limit_is_inclusive_and_includes_inventory(
    policy: QualificationPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert planner.MAX_SELECTION_BYTES == 4 * 1024 * 1024
    result = plan_tests(policy, profile="empty", changed_paths=())
    size = len(
        json.dumps(
            asdict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    monkeypatch.setattr(planner, "MAX_SELECTION_BYTES", size)
    assert plan_tests(policy, profile="empty", changed_paths=()) == result
    monkeypatch.setattr(planner, "MAX_SELECTION_BYTES", size - 1)
    with pytest.raises(ConfigurationError, match="test selection exceeds"):
        plan_tests(policy, profile="empty", changed_paths=())


def test_reason_expansion_fails_before_returning_a_partial_plan(
    policy: QualificationPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(planner, "MAX_SELECTION_BYTES", 256)
    with pytest.raises(ConfigurationError, match="selection reasons exceed"):
        plan_tests(policy, profile="development", changed_paths=("schema/a", "schema/b"))
