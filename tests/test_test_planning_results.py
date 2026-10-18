"""Closed result receipt parsing, immutable models, and complete encoding bounds."""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError, replace
from typing import Any, cast

import pytest

from yaga.errors import ConfigurationError, InputError
from yaga.test_planning import results as result_module
from yaga.test_planning.results import (
    MAX_ATTEMPT,
    MAX_SUMMARY_BYTES,
    CellResult,
    ResultOutcome,
    encode_results,
    parse_results_document,
    results_document,
)
from yaga.test_planning.results import (
    TestResults as Results,
)


@pytest.fixture
def results() -> Results:
    return Results(
        plan_digest="a" * 64,
        policy_sha256="b" * 64,
        bindings_sha256="c" * 64,
        execution_commit="1" * 40,
        execution_tree="2" * 40,
        run_id="github:run-123.abc_4",
        attempt=1,
        results=(
            CellResult("unit", "py312", "pytest", ResultOutcome.PASSED),
            CellResult(
                "frontend",
                "node",
                "vitest",
                ResultOutcome.FAILED,
                summary="An assertion failed.",
                evidence_sha256="d" * 64,
            ),
        ),
    )


def test_exact_closed_document_and_optional_fields(results: Results) -> None:
    document = results_document(results)
    assert set(document) == {
        "test-results-version",
        "plan_digest",
        "policy_sha256",
        "bindings_sha256",
        "execution_commit",
        "execution_tree",
        "run_id",
        "attempt",
        "results",
    }
    assert document["test-results-version"] == 1
    assert document["results"] == [
        {
            "suite": "frontend",
            "variant": "node",
            "binding": "vitest",
            "outcome": "failed",
            "summary": "An assertion failed.",
            "evidence_sha256": "d" * 64,
        },
        {"suite": "unit", "variant": "py312", "binding": "pytest", "outcome": "passed"},
    ]
    restored = parse_results_document(json.loads(encode_results(results)))
    assert results_document(restored) == document
    assert encode_results(results) == encode_results(
        replace(results, results=tuple(reversed(results.results)))
    )
    assert encode_results(results).endswith(b"\n")


@pytest.mark.parametrize("outcome", list(ResultOutcome))
def test_every_closed_outcome_survives_round_trip(results: Results, outcome: ResultOutcome) -> None:
    changed = replace(results, results=(replace(results.results[0], outcome=outcome),))
    assert parse_results_document(results_document(changed)) == changed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("test-results-version", True),
        ("test-results-version", 2),
        ("plan_digest", None),
        ("plan_digest", "A" * 64),
        ("plan_digest", "a" * 63),
        ("policy_sha256", False),
        ("policy_sha256", "b" * 65),
        ("bindings_sha256", None),
        ("bindings_sha256", 123),
        ("execution_commit", "A" * 40),
        ("execution_commit", "a" * 39),
        ("execution_commit", True),
        ("execution_tree", "b" * 64),
        ("execution_tree", None),
        ("attempt", True),
        ("attempt", 0),
        ("attempt", -1),
        ("attempt", MAX_ATTEMPT + 1),
        ("attempt", 1.0),
        ("attempt", "1"),
        ("run_id", ""),
        ("run_id", "-leading"),
        ("run_id", "bad\nrun"),
        ("run_id", "bad/run"),
        ("run_id", "a" * 129),
        ("run_id", "日本"),
        ("run_id", True),
        ("results", {}),
        ("results", ()),
    ],
)
def test_invalid_envelope_fields_are_input_errors(
    results: Results, field: str, value: object
) -> None:
    document = results_document(results)
    document[field] = value
    with pytest.raises(InputError):
        parse_results_document(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("suite", None),
        ("suite", "Unit"),
        ("suite", "shell;command"),
        ("suite", "a" * 65),
        ("variant", []),
        ("variant", True),
        ("binding", "../command"),
        ("binding", 42),
        ("outcome", "skipped"),
        ("outcome", "success"),
        ("outcome", True),
        ("summary", None),
        ("summary", False),
        ("summary", "a" * 1001),
        ("summary", "\ud800"),
        ("summary", "日" * 334),
        ("evidence_sha256", None),
        ("evidence_sha256", "d" * 63),
        ("evidence_sha256", "D" * 64),
        ("evidence_sha256", {}),
    ],
)
def test_invalid_cell_fields_are_input_errors(results: Results, field: str, value: object) -> None:
    document = results_document(results)
    document["results"][0][field] = value
    with pytest.raises(InputError):
        parse_results_document(document)


@pytest.mark.parametrize("where", ["envelope", "cell"])
def test_unknown_and_missing_fields_are_rejected(results: Results, where: str) -> None:
    document = results_document(results)
    target = document if where == "envelope" else document["results"][0]
    target["command"] = "echo untrusted"
    with pytest.raises(InputError, match="fields"):
        parse_results_document(document)
    del target["command"]
    del target["attempt" if where == "envelope" else "binding"]
    with pytest.raises(InputError, match="fields"):
        parse_results_document(document)


def test_duplicate_cells_rejected_even_with_different_binding_or_outcome(results: Results) -> None:
    for mutation in ({}, {"binding": "other"}, {"outcome": "passed"}):
        document = results_document(results)
        duplicate = copy.deepcopy(document["results"][0])
        duplicate.update(mutation)
        document["results"].append(duplicate)
        with pytest.raises(InputError):
            parse_results_document(document)


def test_cell_count_limit_and_empty_results_are_distinct(results: Results) -> None:
    document = results_document(results)
    document["results"] = [None] * 513
    with pytest.raises(InputError, match="bounded"):
        parse_results_document(document)
    document["results"] = []
    assert parse_results_document(document).results == ()


def test_summary_utf8_boundary_is_exact_and_preserved(results: Results) -> None:
    summary = "日" * 333 + "x"
    assert len(summary.encode()) == MAX_SUMMARY_BYTES
    changed = replace(results, results=(replace(results.results[0], summary=summary),))
    assert parse_results_document(results_document(changed)) == changed
    assert summary.encode() in encode_results(changed)


def test_direct_models_are_frozen_and_reject_invalid_values(results: Results) -> None:
    with pytest.raises(FrozenInstanceError):
        cast(Any, results).attempt = 2
    with pytest.raises(FrozenInstanceError):
        cast(Any, results.results[0]).suite = "other"
    with pytest.raises(ValueError):
        replace(results, results=[results.results[0]])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        replace(results, attempt=True)
    with pytest.raises(ValueError):
        replace(results, bindings_sha256=cast(str, None))
    with pytest.raises(ValueError):
        replace(results, results=(results.results[0], results.results[0]))
    with pytest.raises(ValueError):
        replace(results.results[0], outcome=cast(ResultOutcome, "passed"))
    with pytest.raises(ValueError):
        replace(results.results[0], summary="\ud800")


def test_mutated_models_are_revalidated_by_public_encoder(results: Results) -> None:
    object.__setattr__(results.results[0], "outcome", "passed")
    with pytest.raises(InputError, match="valid"):
        results_document(results)


def test_source_supports_sha256_object_format_and_attempt_upper_boundary(results: Results) -> None:
    changed = replace(
        results, execution_commit="1" * 64, execution_tree="2" * 64, attempt=MAX_ATTEMPT
    )
    assert parse_results_document(results_document(changed)) == replace(
        changed, results=tuple(sorted(changed.results, key=lambda r: r.suite))
    )


@pytest.mark.parametrize("value", [None, [], "{}", b"{}", True])
def test_top_level_types_are_strict(value: Any) -> None:
    with pytest.raises(InputError):
        parse_results_document(value)
    with pytest.raises(InputError):
        results_document(value)


def test_string_and_dictionary_subclasses_are_rejected(results: Results) -> None:
    class Token(str):
        pass

    class Document(dict):
        pass

    document = results_document(results)
    with pytest.raises(InputError):
        parse_results_document(Document(document))
    document["run_id"] = Token("run")
    with pytest.raises(InputError):
        parse_results_document(document)


def test_complete_byte_limit_includes_newline_and_applies_to_construction(
    results: Results, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded = encode_results(results)
    document = results_document(results)
    monkeypatch.setattr(result_module, "MAX_PLAN_BYTES", len(encoded))
    assert encode_results(results) == encoded
    assert results_document(parse_results_document(document)) == document
    monkeypatch.setattr(result_module, "MAX_PLAN_BYTES", len(encoded) - 1)
    with pytest.raises(ConfigurationError, match="byte limit"):
        encode_results(results)
    with pytest.raises(ConfigurationError, match="byte limit"):
        parse_results_document(document)
    with pytest.raises(ConfigurationError, match="byte limit"):
        replace(results)
