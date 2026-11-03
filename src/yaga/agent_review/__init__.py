"""Provider-neutral Agent review policy models and validation."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yaga.agent_review.evaluation import LensOutcome, ReviewAggregate, evaluate_policy
    from yaga.agent_review.plan import ReviewPlan, ReviewPlanItem, build_plan, render_plan
    from yaga.agent_review.policy import AgentReviewPolicy, BotIdentity, ReviewLens, load_policy
    from yaga.agent_review.results import (
        AgentReviewResults,
        LensResult,
        build_pending_results,
        evaluate_results,
        load_results,
        render_evaluation,
        render_results_template,
    )
    from yaga.agent_review.snapshot import (
        PullRequestCheckRunRecord,
        PullRequestIssueCommentRecord,
        PullRequestReactionRecord,
        PullRequestReviewCommentRecord,
        PullRequestReviewRecord,
        PullRequestReviewSnapshot,
        SnapshotEvaluation,
        evaluate_pull_request_snapshot,
        is_review_actively_in_progress,
        load_pull_request_snapshot,
        parse_pull_request_snapshot,
    )

__all__ = [
    "AgentReviewPolicy",
    "AgentReviewResults",
    "BotIdentity",
    "LensOutcome",
    "LensResult",
    "PullRequestCheckRunRecord",
    "PullRequestIssueCommentRecord",
    "PullRequestReactionRecord",
    "PullRequestReviewCommentRecord",
    "PullRequestReviewRecord",
    "PullRequestReviewSnapshot",
    "ReviewAggregate",
    "ReviewLens",
    "ReviewPlan",
    "ReviewPlanItem",
    "SnapshotEvaluation",
    "build_pending_results",
    "build_plan",
    "evaluate_policy",
    "evaluate_pull_request_snapshot",
    "evaluate_results",
    "is_review_actively_in_progress",
    "load_policy",
    "load_pull_request_snapshot",
    "load_results",
    "parse_pull_request_snapshot",
    "render_evaluation",
    "render_plan",
    "render_results_template",
]

_EXPORTS_BY_NAME: dict[str, str] = {
    "LensOutcome": "yaga.agent_review.evaluation",
    "ReviewAggregate": "yaga.agent_review.evaluation",
    "evaluate_policy": "yaga.agent_review.evaluation",
    "ReviewPlan": "yaga.agent_review.plan",
    "ReviewPlanItem": "yaga.agent_review.plan",
    "build_plan": "yaga.agent_review.plan",
    "render_plan": "yaga.agent_review.plan",
    "AgentReviewPolicy": "yaga.agent_review.policy",
    "BotIdentity": "yaga.agent_review.policy",
    "ReviewLens": "yaga.agent_review.policy",
    "load_policy": "yaga.agent_review.policy",
    "AgentReviewResults": "yaga.agent_review.results",
    "LensResult": "yaga.agent_review.results",
    "build_pending_results": "yaga.agent_review.results",
    "evaluate_results": "yaga.agent_review.results",
    "load_results": "yaga.agent_review.results",
    "render_evaluation": "yaga.agent_review.results",
    "render_results_template": "yaga.agent_review.results",
    "PullRequestCheckRunRecord": "yaga.agent_review.snapshot",
    "PullRequestIssueCommentRecord": "yaga.agent_review.snapshot",
    "PullRequestReactionRecord": "yaga.agent_review.snapshot",
    "PullRequestReviewCommentRecord": "yaga.agent_review.snapshot",
    "PullRequestReviewRecord": "yaga.agent_review.snapshot",
    "PullRequestReviewSnapshot": "yaga.agent_review.snapshot",
    "SnapshotEvaluation": "yaga.agent_review.snapshot",
    "evaluate_pull_request_snapshot": "yaga.agent_review.snapshot",
    "is_review_actively_in_progress": "yaga.agent_review.snapshot",
    "load_pull_request_snapshot": "yaga.agent_review.snapshot",
    "parse_pull_request_snapshot": "yaga.agent_review.snapshot",
}


def __getattr__(name: str) -> object:
    module_name = _EXPORTS_BY_NAME.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value
