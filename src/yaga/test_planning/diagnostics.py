"""Pure recorded-selection diagnostics and strict offline plan decoding.

These summaries inspect recorded reasons; they do not rematch paths, prove
dependency coverage, authenticate a plan's origin, or report execution results.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, cast

from yaga.errors import ConfigurationError, InputError
from yaga.path_patterns import MAX_PATH_BYTES
from yaga.test_planning.limits import (
    MAX_CAPABILITIES,
    MAX_CHANGED_PATHS,
    MAX_NEEDS,
    MAX_PLAN_BYTES,
    MAX_SUITES,
    MAX_TAGS,
    MAX_TOKEN_BYTES,
    MAX_VARIANTS,
)
from yaga.test_planning.models import SelectionMode, Suite
from yaga.test_planning.reporting import test_plan_document
from yaga.test_planning.reports import PlannedTests
from yaga.test_planning.selection import SelectedSuite, SelectionPlan, SelectionReason
from yaga.test_planning.source import PlanSource

TEST_SELECTION_DIAGNOSTICS_VERSION = 1
_PLAN_FIELDS = {
    "test-plan-version",
    "policy_sha256",
    "bindings_sha256",
    "profile",
    "requested_mode",
    "effective_mode",
    "source",
    "changed_paths",
    "inventory",
    "selected",
    "reasons",
}
_SOURCE_FIELDS = {
    "base_commit",
    "head_commit",
    "comparison_commit",
    "execution_commit",
    "execution_tree",
}
_DIAGNOSTIC_FIELDS = {
    "test-selection-diagnostics-version",
    "plan_digest",
    "policy_sha256",
    "bindings_sha256",
    "source",
    "profile",
    "requested_mode",
    "effective_mode",
    "selection_breadth",
    "fallback_cause",
    "counts",
    "full_trigger_paths",
    "unknown_paths",
    "no_impact_paths",
    "paths_without_recorded_classification",
    "rules",
    "comparison",
}


def parse_plan_document(value: object) -> PlannedTests:
    """Reconstruct a closed v1 document and verify its canonical semantic digest.

    The caller owns bounded file/JSON acquisition, including duplicate JSON-key
    rejection. This function accepts ordinary decoded JSON values only, bounds
    their canonical encoding including a newline, and never reads source files.
    A matching digest establishes consistency, not trusted origin or correctness
    of the supplied policy's dependency mappings.
    """
    envelope = _object(value, {"test-plan-version", "plan_digest", "plan"})
    _version(envelope["test-plan-version"])
    _string(envelope["plan_digest"], maximum=64)
    plan = _object(envelope["plan"], _PLAN_FIELDS)
    _version(plan["test-plan-version"])
    _encode(envelope)
    paths = plan["changed_paths"]
    bindings = plan["bindings_sha256"]
    try:
        result = PlannedTests(
            policy_sha256=_string(plan["policy_sha256"], maximum=64),
            bindings_sha256=None if bindings is None else _string(bindings, maximum=64),
            source=_source(plan["source"]),
            selection=SelectionPlan(
                profile=_string(plan["profile"], maximum=MAX_TOKEN_BYTES),
                requested_mode=SelectionMode(_string(plan["requested_mode"], maximum=16)),
                effective_mode=SelectionMode(_string(plan["effective_mode"], maximum=16)),
                changed_paths=None
                if paths is None
                else _strings(paths, maximum=MAX_CHANGED_PATHS, width=MAX_PATH_BYTES),
                inventory=tuple(_suite(item) for item in _array(plan["inventory"], MAX_SUITES)),
                selected=tuple(_selected(item) for item in _array(plan["selected"], MAX_SUITES)),
                reasons=tuple(
                    _reason(item) for item in _array(plan["reasons"], MAX_PLAN_BYTES // 64)
                ),
            ),
        )
    except (TypeError, ValueError) as error:
        raise InputError("invalid version-one test plan semantics") from error
    if test_plan_document(result) != envelope:
        raise InputError("test plan document does not match its canonical semantics and digest")
    return result


def diagnose_selection(
    planned: PlannedTests, *, baseline_suites: tuple[str, ...] | None = None
) -> dict[str, Any]:
    """Summarize recorded reasons without rematching policy or inferring coverage.

    ``None`` means no baseline was supplied; an empty tuple means the baseline
    explicitly selected nothing. Baseline suite IDs must be unique catalog IDs.
    Path lists are null when a revision-only plan has no path comparison. Paths
    bypassed by an early full decision remain explicitly unclassified here.
    """
    if type(planned) is not PlannedTests:
        raise InputError("selection diagnostics require a PlannedTests model")
    try:
        PlannedTests(
            planned.policy_sha256, planned.bindings_sha256, planned.source, planned.selection
        )
    except (TypeError, ValueError) as error:
        raise InputError("selection diagnostics require a valid test plan") from error
    original = test_plan_document(planned)
    planned = parse_plan_document(original)
    selection = planned.selection
    catalog = {suite.id for suite in selection.inventory}
    selected = {suite.suite for suite in selection.selected}
    comparison = _comparison(baseline_suites, catalog, selected)
    grouped: dict[str, set[str]] = {}
    rules: dict[str, tuple[set[str], set[str]]] = {}
    classified: set[str] = set()
    for reason in selection.reasons:
        if reason.path is not None:
            classified.add(reason.path)
            grouped.setdefault(reason.code, set()).add(reason.path)
        if reason.code == "test.rule":
            # The source-bound model already requires all three fields.
            assert reason.rule is not None and reason.path is not None and reason.suite is not None
            paths, suites = rules.setdefault(reason.rule, (set(), set()))
            paths.add(reason.path)
            suites.add(reason.suite)
    codes = {reason.code for reason in selection.reasons}
    cause = next(
        (
            name
            for name in ("profile-full", "full-trigger", "unknown-path")
            if f"test.{name}" in codes
        ),
        "selective",
    )
    changed = selection.changed_paths
    document = {
        "test-selection-diagnostics-version": TEST_SELECTION_DIAGNOSTICS_VERSION,
        "plan_digest": original["plan_digest"],
        "policy_sha256": planned.policy_sha256,
        "bindings_sha256": planned.bindings_sha256,
        "source": asdict(planned.source),
        "profile": selection.profile,
        "requested_mode": selection.requested_mode.value,
        "effective_mode": selection.effective_mode.value,
        "selection_breadth": "empty"
        if not selected
        else "all"
        if selected == catalog
        else "partial",
        "fallback_cause": cause,
        "counts": {
            "catalog_suites": len(catalog),
            "selected_suites": len(selected),
            "catalog_cells": sum(len(suite.variants) for suite in selection.inventory),
            "selected_cells": sum(len(suite.variants) for suite in selection.selected),
            "changed_paths": None if changed is None else len(changed),
        },
        "full_trigger_paths": None
        if changed is None
        else sorted(grouped.get("test.full-trigger", ())),
        "unknown_paths": None if changed is None else sorted(grouped.get("test.unknown-path", ())),
        "no_impact_paths": None if changed is None else sorted(grouped.get("test.no-impact", ())),
        "paths_without_recorded_classification": None
        if changed is None
        else sorted(set(changed) - classified),
        "rules": [
            {
                "rule": rule,
                "paths": sorted(paths),
                "suites": sorted(suites),
                "path_count": len(paths),
                "suite_count": len(suites),
            }
            for rule, (paths, suites) in sorted(rules.items())
        ],
        "comparison": comparison,
    }
    encode_diagnostics(document)
    return document


def encode_diagnostics(document: dict[str, Any]) -> bytes:
    """Encode a complete v1 diagnostic document with a bounded final newline.

    Use ``diagnose_selection`` to establish the document's semantic relationship
    to a validated plan. Encoding does not authenticate that relationship.
    """
    value = _object(document, _DIAGNOSTIC_FIELDS)
    _version(value["test-selection-diagnostics-version"])
    return _encode(value)


def _comparison(
    baseline: tuple[str, ...] | None, catalog: set[str], selected: set[str]
) -> dict[str, list[str]] | None:
    if baseline is None:
        return None
    if (
        type(baseline) is not tuple
        or len(baseline) > MAX_SUITES
        or any(type(suite) is not str or suite not in catalog for suite in baseline)
        or len(set(baseline)) != len(baseline)
    ):
        raise InputError("baseline suites must be a bounded tuple of unique catalog suite IDs")
    return {
        "baseline": sorted(baseline),
        "selected": sorted(selected),
        "additional": sorted(selected - set(baseline)),
        "missing": sorted(set(baseline) - selected),
    }


def _source(value: object) -> PlanSource:
    source = _object(value, _SOURCE_FIELDS)
    return PlanSource(
        base_commit=_optional_string(source["base_commit"]),
        head_commit=_optional_string(source["head_commit"]),
        comparison_commit=_optional_string(source["comparison_commit"]),
        execution_commit=_string(source["execution_commit"], maximum=64),
        execution_tree=_string(source["execution_tree"], maximum=64),
    )


def _suite(value: object) -> Suite:
    suite = _object(value, {"id", "binding", "tags", "variants", "needs", "requires"})
    return Suite(
        id=_string(suite["id"], maximum=MAX_TOKEN_BYTES),
        binding=_string(suite["binding"], maximum=MAX_TOKEN_BYTES),
        tags=_strings(suite["tags"], maximum=MAX_TAGS),
        variants=_strings(suite["variants"], maximum=MAX_VARIANTS),
        needs=_strings(suite["needs"], maximum=MAX_NEEDS),
        requires=_strings(suite["requires"], maximum=MAX_CAPABILITIES),
    )


def _selected(value: object) -> SelectedSuite:
    selected = _object(value, {"suite", "variants"})
    return SelectedSuite(
        _string(selected["suite"], maximum=MAX_TOKEN_BYTES),
        _strings(selected["variants"], maximum=MAX_VARIANTS),
    )


def _reason(value: object) -> SelectionReason:
    reason = _object(value, {"code", "suite", "rule", "path", "required_by"})
    return SelectionReason(
        code=_string(reason["code"], maximum=64),
        suite=_optional_string(reason["suite"]),
        rule=_optional_string(reason["rule"]),
        path=_optional_string(reason["path"], maximum=MAX_PATH_BYTES),
        required_by=_optional_string(reason["required_by"]),
    )


def _object(value: object, fields: set[str]) -> dict[str, Any]:
    if (
        type(value) is not dict
        or len(value) != len(fields)
        or any(type(key) is not str for key in value)
        or value.keys() != fields
    ):
        raise InputError("test document requires exactly its versioned object fields")
    return cast(dict[str, Any], value)


def _array(value: object, maximum: int) -> list[Any]:
    if type(value) is not list or len(value) > maximum:
        raise InputError("test document array has an invalid type or exceeds its entry limit")
    return value


def _string(value: object, *, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum:
        raise InputError("test document string has an invalid type or exceeds its length limit")
    return value


def _strings(value: object, *, maximum: int, width: int = MAX_TOKEN_BYTES) -> tuple[str, ...]:
    return tuple(_string(item, maximum=width) for item in _array(value, maximum))


def _optional_string(value: object, *, maximum: int = 64) -> str | None:
    return None if value is None else _string(value, maximum=maximum)


def _version(value: object) -> None:
    if type(value) is not int or value != 1:
        raise InputError("unsupported test document version")


def _encode(document: dict[str, Any]) -> bytes:
    # Bound and restrict ordinary JSON containers before handing them to the
    # encoder; subclasses, cycles, excessive nesting and non-finite numbers are
    # not part of these contracts. String sizes are checked before encoding.
    _json_types(document, depth=0, budget=[MAX_PLAN_BYTES])
    chunks: list[bytes] = []
    remaining = MAX_PLAN_BYTES - 1
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        for chunk in encoder.iterencode(document):
            encoded = chunk.encode("utf-8")
            remaining -= len(encoded)
            if remaining < 0:
                raise ConfigurationError(
                    f"test diagnostics exceed the hard {MAX_PLAN_BYTES}-byte limit"
                )
            chunks.append(encoded)
    except (ValueError, UnicodeError) as error:
        raise InputError("test document is not valid bounded UTF-8 JSON") from error
    return b"".join(chunks) + b"\n"


def _json_types(value: object, *, depth: int, budget: list[int]) -> None:
    budget[0] -= 1
    if depth > 8 or budget[0] < 0:
        raise InputError("test document exceeds the JSON structure limit")
    if type(value) is str:
        if len(value) > MAX_PLAN_BYTES:
            raise ConfigurationError(
                f"test diagnostics exceed the hard {MAX_PLAN_BYTES}-byte limit"
            )
    elif value is None or type(value) is bool:
        return
    elif type(value) is int:
        if not 0 <= value <= MAX_PLAN_BYTES:
            raise InputError("test document integer is outside its bounded range")
    elif type(value) is list:
        if len(value) > budget[0]:
            raise InputError("test document exceeds the JSON structure limit")
        for item in value:
            _json_types(item, depth=depth + 1, budget=budget)
    elif type(value) is dict:
        if len(value) > budget[0] or any(type(key) is not str for key in value):
            raise InputError("test document requires bounded JSON string keys")
        for key, item in value.items():
            _json_types(key, depth=depth + 1, budget=budget)
            _json_types(item, depth=depth + 1, budget=budget)
    else:
        raise InputError("test document contains a non-JSON value")
