"""Installed pull-request review check orchestration and bounded reporting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from yaga.agent_review.evaluation import LensOutcome
from yaga.agent_review.plan import build_plan
from yaga.agent_review.policy import MAX_WAIT_SECONDS, load_policy
from yaga.agent_review.remote import (
    POLL_INTERVAL_SECONDS,
    STARTUP_GRACE_SECONDS,
    SubmittedPullRequestReview,
    fetch_pull_request_review_snapshot,
    monotonic_seconds,
    parse_pull_request_reference,
    sleep_for_review_poll,
    submit_pull_request_review,
)
from yaga.agent_review.results import render_evaluation
from yaga.agent_review.snapshot import (
    SnapshotEvaluation,
    evaluate_pull_request_snapshot,
    is_review_actively_in_progress,
    load_pull_request_snapshot,
)
from yaga.errors import InputError, YagaError, safe_error_text


class ReviewOutputFormat(StrEnum):
    """Supported pull-request review report formats."""

    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


class ReviewSubmitMode(StrEnum):
    """Supported pull-request review submission modes."""

    NONE = "none"
    APPROVE = "approve"
    REQUEST_CHANGES = "request-changes"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class CheckedPullRequestReview:
    """A validated pull-request review evaluation and its explicit input labels."""

    evaluation: SnapshotEvaluation
    policy_path: Path
    source: str
    submitted_review: SubmittedPullRequestReview | None = None


def check_pull_request_review_source(
    *,
    policy_path: Path,
    file_path: Path | None = None,
    pull_request: str | None = None,
    github_repository: str | None = None,
    wait_seconds: int | None = None,
    submit_review: ReviewSubmitMode = ReviewSubmitMode.NONE,
) -> CheckedPullRequestReview:
    """Check one explicit review snapshot file or current gh PR against one explicit policy."""
    if sum((file_path is not None, pull_request is not None)) != 1:
        raise InputError("select exactly one pull-request review source: --file or --pr")
    if github_repository is not None and pull_request is None:
        raise InputError("--repo requires a numeric --pr source")
    if wait_seconds is not None:
        if pull_request is None:
            raise InputError("--wait-seconds requires a live --pr source")
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
            raise InputError(f"--wait-seconds must be an integer between 0 and {MAX_WAIT_SECONDS}")
    if not isinstance(submit_review, ReviewSubmitMode):
        raise InputError("--submit-review must be none, approve, request-changes, or both")
    if submit_review is not ReviewSubmitMode.NONE and pull_request is None:
        raise InputError("--submit-review requires a live --pr source")
    reference = (
        parse_pull_request_reference(pull_request, github_repository)
        if pull_request is not None
        else None
    )
    resolved_policy_path = policy_path.expanduser().resolve()
    policy = load_policy(resolved_policy_path)
    plan_digest = build_plan(policy).digest()
    submitted_review: SubmittedPullRequestReview | None = None
    if reference is not None:
        resolved_bots = [lens.resolve_bot() for lens in policy.lenses]
        include_check_runs = any(bool(bot.check_runs) for bot in resolved_bots)
        delay_seconds = max((bot.delay_seconds for bot in resolved_bots), default=0)
        effective_wait_seconds = (
            max((bot.wait_seconds for bot in resolved_bots), default=0)
            if wait_seconds is None
            else wait_seconds
        )
        snapshot = fetch_pull_request_review_snapshot(
            reference,
            plan_digest=plan_digest,
            include_check_runs=include_check_runs,
            delay_seconds=delay_seconds,
        )
        evaluation = evaluate_pull_request_snapshot(policy, snapshot)
        poll_started_at = monotonic_seconds()
        simulated_sleep = 0.0
        elapsed_wait = 0.0
        while (
            elapsed_wait < effective_wait_seconds
            and evaluation.aggregate.state is LensOutcome.PENDING
            and not evaluation.snapshot.draft
            and (
                is_review_actively_in_progress(evaluation, aggregation=policy.aggregation)
                or elapsed_wait < STARTUP_GRACE_SECONDS
            )
        ):
            remaining = effective_wait_seconds - elapsed_wait
            step: int | float = (
                POLL_INTERVAL_SECONDS if remaining >= POLL_INTERVAL_SECONDS else remaining
            )
            sleep_started_at = monotonic_seconds()
            sleep_for_review_poll(step)
            sleep_wall_elapsed = max(0.0, monotonic_seconds() - sleep_started_at)
            if sleep_wall_elapsed < step:
                simulated_sleep += step - sleep_wall_elapsed
            snapshot = fetch_pull_request_review_snapshot(
                reference,
                plan_digest=plan_digest,
                include_check_runs=include_check_runs,
                delay_seconds=0,
            )
            evaluation = evaluate_pull_request_snapshot(policy, snapshot)
            elapsed_wait = max(0.0, monotonic_seconds() - poll_started_at) + simulated_sleep
        desired_event = _desired_review_event(evaluation.aggregate.state, submit_review)
        if desired_event is not None:
            if evaluation.snapshot.state != "open":
                raise InputError("cannot submit a pull-request review on a closed pull request")
            submitted_review = submit_pull_request_review(
                reference,
                head_sha=evaluation.snapshot.head_sha,
                event=desired_event,
                existing_reviews=evaluation.snapshot.reviews,
            )
        source = evaluation.snapshot.url or reference.url
    else:
        assert file_path is not None
        resolved_file = file_path.expanduser().resolve()
        snapshot = load_pull_request_snapshot(resolved_file)
        evaluation = evaluate_pull_request_snapshot(policy, snapshot)
        source = str(resolved_file)
    return CheckedPullRequestReview(
        evaluation=evaluation,
        policy_path=resolved_policy_path,
        source=source,
        submitted_review=submitted_review,
    )


def _desired_review_event(
    state: LensOutcome,
    submit_review: ReviewSubmitMode,
) -> str | None:
    if state is LensOutcome.PASSED and submit_review in {
        ReviewSubmitMode.APPROVE,
        ReviewSubmitMode.BOTH,
    }:
        return "APPROVE"
    if state is LensOutcome.FAILED and submit_review in {
        ReviewSubmitMode.REQUEST_CHANGES,
        ReviewSubmitMode.BOTH,
    }:
        return "REQUEST_CHANGES"
    return None


def review_report_document(checked: CheckedPullRequestReview) -> dict[str, Any]:
    """Return bounded schema-v1 metadata and lens results for a PR review check."""
    evaluation = checked.evaluation
    snapshot = evaluation.snapshot
    aggregate = evaluation.aggregate
    document: dict[str, Any] = {
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
    if checked.submitted_review is not None:
        document["submitted_review"] = checked.submitted_review.document()
    return document


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
    lines = [
        f"Source: {document['source']}",
        f"Pull request: #{snapshot.number} (head {snapshot.head_sha[:12]})",
        rendered,
    ]
    if checked.submitted_review is not None:
        lines.append(
            f"Submitted review: {checked.submitted_review.state} "
            f"({checked.submitted_review.action}) on head {snapshot.head_sha[:12]}"
        )
    return "\n".join(lines)


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
