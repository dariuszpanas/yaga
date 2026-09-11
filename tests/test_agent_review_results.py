"""Tests for the provider-neutral Agent review result contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yaga.agent_review.evaluation import LensOutcome
from yaga.agent_review.policy import AgentReviewPolicy, ReviewLens
from yaga.agent_review.results import (
    AgentReviewResults,
    LensResult,
    evaluate_results,
    load_results,
    render_evaluation,
)
from yaga.errors import ConfigurationError


def review_policy() -> AgentReviewPolicy:
    return AgentReviewPolicy(
        version=1,
        required=("correctness", "security"),
        aggregation="all-required",
        lenses=(
            ReviewLens("correctness", "codex", "Review behavior.", "review"),
            ReviewLens("security", "codex", "Review trust.", "review"),
            ReviewLens("docs", "other-agent", "Review docs.", "advisory"),
        ),
    )


def write_results(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_load_results_preserves_order_and_optional_summaries(tmp_path: Path) -> None:
    results = load_results(
        write_results(
            tmp_path,
            {
                "version": 1,
                "results": [
                    {"lens": "security", "outcome": "pending"},
                    {"lens": "correctness", "outcome": "passed", "summary": "Clear."},
                ],
            },
        )
    )

    assert [result.lens for result in results.results] == ["security", "correctness"]
    assert results.results[0].outcome is LensOutcome.PENDING
    assert results.results[1].summary == "Clear."


def test_evaluate_results_keeps_advisory_findings_non_blocking(tmp_path: Path) -> None:
    results = load_results(
        write_results(
            tmp_path,
            {
                "version": 1,
                "results": [
                    {"lens": "correctness", "outcome": "passed"},
                    {"lens": "security", "outcome": "passed"},
                    {"lens": "docs", "outcome": "failed", "summary": "Stale link."},
                ],
            },
        )
    )

    aggregate = evaluate_results(review_policy(), results)

    assert aggregate.state is LensOutcome.PASSED
    assert aggregate.advisory_failures == ("docs",)
    rendered = render_evaluation(results, aggregate)
    assert "Advisory failures: docs" in rendered


@pytest.mark.parametrize(
    "document",
    [
        {"version": 2, "results": []},
        {
            "version": 1,
            "results": [{"lens": "x", "outcome": "failed"}, {"lens": "x", "outcome": "passed"}],
        },
        {"version": 1, "results": [{"lens": "x", "outcome": "unknown"}]},
        {"version": 1, "results": [{"lens": "x", "outcome": "passed", "summary": ""}]},
        {"version": 1, "results": [{"lens": "x", "outcome": "passed", "extra": 1}]},
        {"version": 1, "results": [{"lens": "Bad Lens", "outcome": "passed"}]},
    ],
)
def test_load_results_rejects_invalid_contract(tmp_path: Path, document: object) -> None:
    with pytest.raises(ConfigurationError):
        load_results(write_results(tmp_path, document))


def test_load_results_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "results.json"
    path.write_text('{"version": 1, "results": [], "results": []}', encoding="utf-8")

    with pytest.raises(ConfigurationError, match="valid JSON"):
        load_results(path)


def test_json_evaluation_is_versioned_and_bounded() -> None:
    results = AgentReviewResults(
        version=1,
        # Constructed directly to exercise rendering independently of file loading.
        results=(LensResult("correctness", LensOutcome.PASSED, "Clear."),),
    )
    aggregate = evaluate_results(review_policy(), results)

    document = json.loads(render_evaluation(results, aggregate, "json"))

    assert document["version"] == 1
    assert document["state"] == "pending"
    assert document["results"][0]["outcome"] == "passed"
