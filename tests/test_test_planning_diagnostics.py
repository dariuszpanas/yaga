"""Recorded selection diagnostics, strict offline plans, and complete byte bounds."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from yaga.errors import ConfigurationError, InputError
from yaga.test_planning import diagnostics
from yaga.test_planning.diagnostics import (
    diagnose_selection,
    encode_diagnostics,
    parse_plan_document,
)
from yaga.test_planning.models import (
    Profile,
    QualificationPolicy,
    SelectionMode,
    SelectionRule,
    Suite,
)
from yaga.test_planning.planner import plan_tests
from yaga.test_planning.policy import load_test_policy
from yaga.test_planning.reporting import test_plan_document as plan_document
from yaga.test_planning.reports import PlannedTests
from yaga.test_planning.source import PlanSource

ROOT = Path(__file__).resolve().parents[1]
SOURCE = PlanSource("1" * 40, "2" * 40, "3" * 40, "2" * 40, "4" * 40)
POLICY_SHA = "a" * 64
BINDINGS_SHA = "b" * 64


@pytest.fixture
def policy() -> QualificationPolicy:
    return QualificationPolicy(
        1,
        suites=(
            Suite("backend", "pytest", tags=("code",), needs=("image",)),
            Suite("frontend", "vitest", tags=("code",)),
            Suite("image", "docker", variants=("amd", "arm")),
        ),
        profiles=(Profile("dev", SelectionMode.SELECTIVE), Profile("full", SelectionMode.FULL)),
        rules=(
            SelectionRule("backend", ("src/**", "src/main.py"), select=("backend",)),
            SelectionRule("frontend", ("web/**",), select=("frontend",)),
            SelectionRule("shared", ("shared/**",), select_tags=("code",)),
        ),
        full_on=("uv.lock",),
        no_impact=("docs/**",),
    )


def _plan(
    policy: QualificationPolicy, paths: tuple[str, ...] | None, *, profile: str = "dev"
) -> PlannedTests:
    source = SOURCE if paths is not None else PlanSource(None, None, None, "2" * 40, "4" * 40)
    return PlannedTests(
        POLICY_SHA, BINDINGS_SHA, source, plan_tests(policy, profile=profile, changed_paths=paths)
    )


def _redigest(document: dict[str, Any]) -> None:
    semantic = json.dumps(
        document["plan"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    document["plan_digest"] = hashlib.sha256(semantic).hexdigest()


def test_partial_selection_retains_identity_counts_and_complete_comparison(
    policy: QualificationPolicy,
) -> None:
    planned = _plan(policy, ("src/main.py", "docs/readme.md"))
    result = diagnose_selection(planned, baseline_suites=("frontend", "backend"))
    assert result["test-selection-diagnostics-version"] == 1
    assert result["plan_digest"] == plan_document(planned)["plan_digest"]
    assert result["policy_sha256"] == POLICY_SHA
    assert result["bindings_sha256"] == BINDINGS_SHA
    assert result["source"] == plan_document(planned)["plan"]["source"]
    assert result["profile"] == "dev"
    assert result["requested_mode"] == result["effective_mode"] == "selective"
    assert result["selection_breadth"] == "partial"
    assert result["fallback_cause"] == "selective"
    assert result["counts"] == {
        "catalog_suites": 3,
        "selected_suites": 2,
        "catalog_cells": 4,
        "selected_cells": 3,
        "changed_paths": 2,
    }
    assert result["comparison"] == {
        "baseline": ["backend", "frontend"],
        "selected": ["backend", "image"],
        "additional": ["image"],
        "missing": ["frontend"],
    }
    assert result["no_impact_paths"] == ["docs/readme.md"]
    assert result["full_trigger_paths"] == result["unknown_paths"] == []
    assert result["paths_without_recorded_classification"] == []
    assert result["rules"] == [
        {
            "rule": "backend",
            "paths": ["src/main.py"],
            "suites": ["backend"],
            "path_count": 1,
            "suite_count": 1,
        }
    ]
    assert json.loads(encode_diagnostics(result)) == result
    assert encode_diagnostics(result).endswith(b"\n")


def test_global_full_retains_bypassed_paths_without_calling_them_known(
    policy: QualificationPolicy,
) -> None:
    result = diagnose_selection(_plan(policy, ("uv.lock", "src/main.py", "new/path")))
    assert result["fallback_cause"] == "full-trigger"
    assert result["selection_breadth"] == "all"
    assert result["effective_mode"] == "full"
    assert result["full_trigger_paths"] == ["uv.lock"]
    assert result["unknown_paths"] == []
    assert result["rules"] == []
    assert result["paths_without_recorded_classification"] == ["new/path", "src/main.py"]


def test_unknown_fallback_preserves_known_rule_and_no_impact_paths(
    policy: QualificationPolicy,
) -> None:
    result = diagnose_selection(_plan(policy, ("new/path", "src/main.py", "docs/index.md")))
    assert result["fallback_cause"] == "unknown-path"
    assert result["unknown_paths"] == ["new/path"]
    assert result["no_impact_paths"] == ["docs/index.md"]
    assert result["rules"][0]["paths"] == ["src/main.py"]
    assert result["paths_without_recorded_classification"] == []


@pytest.mark.parametrize("paths", [None, (), ("src/main.py", "uv.lock")])
def test_full_profile_does_not_infer_path_classifications(
    policy: QualificationPolicy, paths: tuple[str, ...] | None
) -> None:
    result = diagnose_selection(_plan(policy, paths, profile="full"))
    assert result["fallback_cause"] == "profile-full"
    assert result["selection_breadth"] == "all"
    assert result["requested_mode"] == result["effective_mode"] == "full"
    assert result["rules"] == []
    assert result["counts"]["changed_paths"] == (None if paths is None else len(paths))
    assert result["paths_without_recorded_classification"] == (
        None if paths is None else list(paths)
    )
    for field in ("unknown_paths", "no_impact_paths", "full_trigger_paths"):
        assert result[field] == (None if paths is None else [])


def test_all_selected_by_rules_is_selective_and_rule_counts_are_unique(
    policy: QualificationPolicy,
) -> None:
    result = diagnose_selection(_plan(policy, ("shared/a.py", "shared/b.py", "src/main.py")))
    assert result["selection_breadth"] == "all"
    assert result["requested_mode"] == result["effective_mode"] == "selective"
    assert result["fallback_cause"] == "selective"
    assert result["rules"] == [
        {
            "rule": "backend",
            "paths": ["src/main.py"],
            "suites": ["backend"],
            "path_count": 1,
            "suite_count": 1,
        },
        {
            "rule": "shared",
            "paths": ["shared/a.py", "shared/b.py"],
            "suites": ["backend", "frontend"],
            "path_count": 2,
            "suite_count": 2,
        },
    ]


def test_explicit_empty_baseline_differs_from_unavailable(policy: QualificationPolicy) -> None:
    planned = _plan(policy, ("src/main.py",))
    assert diagnose_selection(planned)["comparison"] is None
    assert diagnose_selection(planned, baseline_suites=())["comparison"] == {
        "baseline": [],
        "selected": ["backend", "image"],
        "additional": ["backend", "image"],
        "missing": [],
    }
    empty = diagnose_selection(_plan(policy, ()), baseline_suites=("image",))
    assert empty["selection_breadth"] == "empty"
    assert empty["counts"]["selected_cells"] == 0
    assert empty["comparison"]["missing"] == ["image"]


@pytest.mark.parametrize(
    "baseline",
    [
        [],
        "backend",
        ("backend", "backend"),
        ("unknown",),
        (True,),
        ("backend\nforged",),
        ("backend",) * 129,
        ("Backend",),
    ],
)
def test_baseline_requires_bounded_unique_exact_catalog_ids(
    policy: QualificationPolicy, baseline: Any
) -> None:
    with pytest.raises(InputError, match="baseline"):
        diagnose_selection(_plan(policy, ()), baseline_suites=baseline)


@pytest.mark.parametrize("filename", ["polyglot.toml", "django-ray.toml"])
def test_both_example_policies_round_trip_without_changing_plan_documents(filename: str) -> None:
    policy = load_test_policy(ROOT / "docs/designs/examples/test-planning" / filename).policy
    for profile in policy.profiles:
        paths = None if profile.mode is SelectionMode.FULL else ("unclassified/path",)
        planned = _plan(policy, paths, profile=profile.name)
        document = plan_document(planned)
        original = copy.deepcopy(document)
        restored = parse_plan_document(json.loads(json.dumps(document)))
        assert restored == planned
        assert plan_document(restored) == document == original
        assert diagnose_selection(restored) == diagnose_selection(planned)


def test_preview_identity_and_unicode_paths_survive_round_trip(policy: QualificationPolicy) -> None:
    planned = replace(_plan(policy, ("src/café.py",)), bindings_sha256=None)
    document = plan_document(planned)
    restored = parse_plan_document(document)
    result = diagnose_selection(restored)
    assert result["bindings_sha256"] is None
    assert result["rules"][0]["paths"] == ["src/café.py"]
    assert b"caf\xc3\xa9.py" in encode_diagnostics(result)


def test_archived_31_revision_replay_keeps_original_documents_and_digests() -> None:
    observations = [
        item
        for path in sorted(
            (ROOT / "docs/designs/experiments/django-ray-shadow-replay").glob("observations-*.json")
        )
        for item in json.loads(path.read_bytes())
    ]
    assert len(observations) == 31
    causes: Counter[str] = Counter()
    for observation in observations:
        original = copy.deepcopy(observation["plan"])
        planned = parse_plan_document(original)
        diagnostic = diagnose_selection(
            planned,
            baseline_suites=tuple(
                f"qualification.{dispatch}"
                for dispatch in observation["comparison"]["baseline_selected"]
            ),
        )
        assert plan_document(planned) == observation["plan"] == original
        assert diagnostic["plan_digest"] == observation["comparison"]["plan_digest"]
        assert diagnostic["comparison"]["missing"] == []
        causes[diagnostic["fallback_cause"]] += 1
    assert causes == {"full-trigger": 17, "unknown-path": 11, "selective": 3}


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("test-plan-version",), True),
        (("test-plan-version",), 2),
        (("plan_digest",), "0" * 64),
        (("plan", "test-plan-version"), True),
        (("plan", "requested_mode"), "automatic"),
        (("plan", "effective_mode"), "full"),
        (("plan", "profile"), False),
        (("plan", "bindings_sha256"), "B" * 64),
        (("plan", "policy_sha256"), "short"),
        (("plan", "changed_paths"), ("src/main.py",)),
        (("plan", "changed_paths"), ["../escape"]),
        (("plan", "changed_paths"), ["src/main.py", "src/main.py"]),
        (("plan", "source", "execution_commit"), "5" * 40),
        (("plan", "source", "execution_tree"), None),
        (("plan", "source", "base_commit"), False),
        (("plan", "inventory"), []),
        (("plan", "selected"), []),
        (("plan", "reasons"), [None]),
    ],
)
def test_rejects_invalid_original_plan_shapes_even_with_recomputed_digest(
    policy: QualificationPolicy, path: tuple[str, ...], value: object
) -> None:
    document = plan_document(_plan(policy, ("src/main.py",)))
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    if path != ("plan_digest",):
        _redigest(document)
    with pytest.raises(InputError):
        parse_plan_document(document)


@pytest.mark.parametrize("location", ["envelope", "plan", "source", "suite", "selected", "reason"])
def test_unknown_fields_are_rejected_at_every_object_boundary(
    policy: QualificationPolicy, location: str
) -> None:
    document = plan_document(_plan(policy, ("src/main.py",)))
    plan = document["plan"]
    target = {
        "envelope": document,
        "plan": plan,
        "source": plan["source"],
        "suite": plan["inventory"][0],
        "selected": plan["selected"][0],
        "reason": plan["reasons"][0],
    }[location]
    target["command"] = "untrusted"
    _redigest(document)
    with pytest.raises(InputError, match="fields"):
        parse_plan_document(document)


def test_digest_is_checked_and_noncanonical_orders_are_rejected(
    policy: QualificationPolicy,
) -> None:
    document = plan_document(_plan(policy, ("src/main.py",)))
    document["plan"]["policy_sha256"] = "c" * 64
    with pytest.raises(InputError, match="digest"):
        parse_plan_document(document)
    _redigest(document)
    assert parse_plan_document(document).policy_sha256 == "c" * 64
    document["plan"]["inventory"].reverse()
    _redigest(document)
    with pytest.raises(InputError):
        parse_plan_document(document)


@pytest.mark.parametrize(
    "mutation", ["duplicate-reason", "duplicate-suite", "partial-variants", "missing-prerequisite"]
)
def test_parser_preserves_reason_and_complete_prerequisite_cell_contracts(
    policy: QualificationPolicy, mutation: str
) -> None:
    document = plan_document(_plan(policy, ("src/main.py",)))
    plan = document["plan"]
    if mutation == "duplicate-reason":
        plan["reasons"].append(copy.deepcopy(plan["reasons"][0]))
    elif mutation == "duplicate-suite":
        plan["selected"].append(copy.deepcopy(plan["selected"][0]))
    elif mutation == "partial-variants":
        plan["selected"][0]["variants"] = ["amd"]
    else:
        plan["selected"] = [plan["selected"][1]]
        plan["reasons"] = [
            reason for reason in plan["reasons"] if reason["code"] != "test.prerequisite"
        ]
    _redigest(document)
    with pytest.raises(InputError):
        parse_plan_document(document)


@pytest.mark.parametrize("value", [None, [], True, "{}", b"{}"])
def test_top_level_plan_and_model_types_are_strict(value: Any) -> None:
    with pytest.raises(InputError):
        parse_plan_document(value)
    with pytest.raises(InputError):
        diagnose_selection(value)


def test_invalid_model_is_revalidated_before_diagnostics(policy: QualificationPolicy) -> None:
    planned = _plan(policy, ("src/main.py",))
    object.__setattr__(planned, "selection", replace(planned.selection, effective_mode="full"))
    with pytest.raises(InputError):
        diagnose_selection(planned)


def test_diagnostic_encoding_is_deterministic_and_bounds_final_newline(
    policy: QualificationPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = diagnose_selection(_plan(policy, ("src/main.py",)))
    encoded = encode_diagnostics(result)
    assert encoded == encode_diagnostics(dict(reversed(list(result.items()))))
    monkeypatch.setattr(diagnostics, "MAX_PLAN_BYTES", len(encoded))
    assert encode_diagnostics(result) == encoded
    monkeypatch.setattr(diagnostics, "MAX_PLAN_BYTES", len(encoded) - 1)
    with pytest.raises(ConfigurationError, match="byte limit"):
        encode_diagnostics(result)


def test_plan_input_bound_includes_newline(
    policy: QualificationPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    planned = _plan(policy, ("src/main.py",))
    document = plan_document(planned)
    size = (
        len(
            json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        + 1
    )
    monkeypatch.setattr(diagnostics, "MAX_PLAN_BYTES", size)
    assert parse_plan_document(document) == planned
    monkeypatch.setattr(diagnostics, "MAX_PLAN_BYTES", size - 1)
    with pytest.raises(ConfigurationError, match="byte limit"):
        parse_plan_document(document)


@pytest.mark.parametrize("bad", [float("nan"), object(), ("tuple",), "\ud800"])
def test_encoder_rejects_non_json_or_invalid_unicode_values(
    policy: QualificationPolicy, bad: object
) -> None:
    result = diagnose_selection(_plan(policy, ()))
    result["comparison"] = bad
    with pytest.raises(InputError):
        encode_diagnostics(result)


def test_encoder_rejects_cycles_and_closed_envelope_changes(policy: QualificationPolicy) -> None:
    result = diagnose_selection(_plan(policy, ()))
    result["comparison"] = result
    with pytest.raises(InputError, match="structure"):
        encode_diagnostics(result)
    result["extra"] = True
    with pytest.raises(InputError, match="fields"):
        encode_diagnostics(result)


def test_expansion_fails_completely_when_diagnostics_exceed_limit(
    policy: QualificationPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    planned = _plan(policy, ())
    original = diagnostics.encode_diagnostics

    def limited(document: dict[str, Any]) -> bytes:
        size_without_newline = len(
            json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        monkeypatch.setattr(diagnostics, "MAX_PLAN_BYTES", size_without_newline)
        return original(document)

    monkeypatch.setattr(diagnostics, "encode_diagnostics", limited)
    with pytest.raises(ConfigurationError, match="byte limit"):
        diagnose_selection(planned)


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("changed_paths", ["src/main.py"] * 2049),
        ("inventory", [None] * 129),
        ("selected", [None] * 129),
        ("reasons", [None] * (diagnostics.MAX_PLAN_BYTES // 64 + 1)),
    ],
)
def test_parser_rejects_oversized_arrays_before_constructing_models(
    policy: QualificationPolicy, key: str, replacement: object
) -> None:
    document = plan_document(_plan(policy, ()))
    document["plan"][key] = replacement
    with pytest.raises(InputError, match="entry limit"):
        parse_plan_document(document)


@pytest.mark.parametrize("field", ["tags", "variants", "needs", "requires"])
def test_parser_rejects_wrong_nested_types_and_duplicate_metadata(
    policy: QualificationPolicy, field: str
) -> None:
    document = plan_document(_plan(policy, ()))
    suite = document["plan"]["inventory"][0]
    suite[field] = [True]
    with pytest.raises(InputError):
        parse_plan_document(document)
    suite[field] = ["repeated", "repeated"]
    _redigest(document)
    with pytest.raises(InputError):
        parse_plan_document(document)


def test_subclasses_are_not_treated_as_json_or_baseline_tokens(policy: QualificationPolicy) -> None:
    class Token(str):
        pass

    document = plan_document(_plan(policy, ()))
    document["plan"]["profile"] = Token("dev")
    with pytest.raises(InputError):
        parse_plan_document(document)
    with pytest.raises(InputError):
        diagnose_selection(_plan(policy, ()), baseline_suites=(cast(str, Token("backend")),))
