"""Installed pull-request review check orchestration and bounded reporting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from yaga.agent_review.evaluation import LensOutcome
from yaga.agent_review.plan import build_plan
from yaga.agent_review.policy import load_policy
from yaga.agent_review.remote import (
    fetch_pull_request_review_snapshot,
    parse_pull_request_reference,
)
from yaga.agent_review.results import render_evaluation
from yaga.agent_review.snapshot import (
    SnapshotEvaluation,
    evaluate_pull_request_snapshot,
    load_pull_request_snapshot,
)
from yaga.errors import InputError, YagaError, safe_error_text


class ReviewOutputFormat(StrEnum):
    """Supported pull-request review report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


@dataclass(frozen=True, slots=True)
class CheckedPullRequestReview:
    """A validated pull-request review evaluation and its explicit input labels."""

    evaluation: SnapshotEvaluation
    policy_path: Path
    source: str


def check_pull_request_review_source(
    *,
    policy_path: Path,
    file_path: Path | None = None,
    pull_request: str | None = None,
    github_repository: str | None = None,
) -> CheckedPullRequestReview:
    """Check one explicit review snapshot file or current gh PR against one explicit policy."""
    if sum((file_path is not None, pull_request is not None)) != 1:
        raise InputError("select exactly one pull-request review source: --file or --pr")
    if github_repository is not None and pull_request is None:
        raise InputError("--repo requires a numeric --pr source")
    reference = (
        parse_pull_request_reference(pull_request, github_repository)
        if pull_request is not None
        else None
    )
    resolved_policy_path = policy_path.expanduser().resolve()
    policy = load_policy(resolved_policy_path)
    plan_digest = build_plan(policy).digest()
    if reference is not None:
        snapshot = fetch_pull_request_review_snapshot(reference, plan_digest=plan_digest)
        source = snapshot.url or reference.url
    else:
        assert file_path is not None
        resolved_file = file_path.expanduser().resolve()
        snapshot = load_pull_request_snapshot(resolved_file)
        source = str(resolved_file)
    evaluation = evaluate_pull_request_snapshot(policy, snapshot)
    return CheckedPullRequestReview(
        evaluation=evaluation,
        policy_path=resolved_policy_path,
        source=source,
    )


def review_report_document(checked: CheckedPullRequestReview) -> dict[str, Any]:
    """Return bounded schema-v1 metadata and lens results for a PR review check."""
    evaluation = checked.evaluation
    snapshot = evaluation.snapshot
    aggregate = evaluation.aggregate
    return {
        "schema_version": 1,
        "kind": "pull_request_review",
        "status": aggregate.state.value,
        "valid": aggregate.state is LensOutcome.PASSED,
        "policy_path": safe_error_text(checked.policy_path, maximum=1000),
        "source": safe_error_text(checked.source, maximum=1000),
        "pull_request_number": snapshot.number,
        "head_sha": snapshot.head_sha,
        "base_sha": snapshot.base_sha,
        "plan_digest": evaluation.results.plan_digest,
        "blocking_failures": list(aggregate.blocking_failures),
        "blocking_pending": list(aggregate.blocking_pending),
        "advisory_failures": list(aggregate.advisory_failures),
        "advisory_pending": list(aggregate.advisory_pending),
        "results": [
            {
                "lens": item.lens,
                "outcome": item.outcome.value,
                "summary": (
                    safe_error_text(item.summary, maximum=500) if item.summary is not None else None
                ),
            }
            for item in evaluation.results.results
        ],
    }


def render_pull_request_review_report(
    checked: CheckedPullRequestReview,
    output_format: ReviewOutputFormat,
) -> str:
    """Render a deterministic text, JSON, or GitHub report for a PR review check."""
    document = review_report_document(checked)
    if output_format is ReviewOutputFormat.JSON:
        return json.dumps(document, ensure_ascii=False, indent=2)
    evaluation = checked.evaluation
    snapshot = evaluation.snapshot
    if output_format is ReviewOutputFormat.GITHUB:
        return render_evaluation(evaluation.results, evaluation.aggregate, "github")
    rendered = render_evaluation(evaluation.results, evaluation.aggregate, "text")
    return "\n".join(
        (
            f"Source: {document['source']}",
            f"Pull request: #{snapshot.number} (head {snapshot.head_sha[:12]})",
            rendered,
        )
    )


def render_pull_request_review_error(
    error: YagaError,
    output_format: ReviewOutputFormat,
) -> str:
    """Keep operational and configuration failures distinct from review outcomes."""
    message = safe_error_text(error, maximum=500)
    if output_format is ReviewOutputFormat.JSON:
        return json.dumps(
            {
                "schema_version": 1,
                "kind": "pull_request_review",
                "status": "error",
                "valid": False,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        )
    if output_format is ReviewOutputFormat.GITHUB:
        title = _escape(safe_error_text("YAGA PR review", maximum=120)).replace(":", "%3A")
        escaped = _escape(f"{error.kind} error: {message}")
        return f"::error title={title}::{escaped}"
    return f"YAGA {error.kind} error: {message}"


def _escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
