"""Tests for provider-neutral named-lens execution plans."""

from __future__ import annotations

import json

from yaga.agent_review.plan import build_plan, render_plan
from yaga.agent_review.policy import AgentReviewPolicy, ReviewLens


def review_policy() -> AgentReviewPolicy:
    return AgentReviewPolicy(
        version=1,
        required=("correctness",),
        aggregation="all-required",
        lenses=(
            ReviewLens("correctness", "codex", "review", "inline"),
            ReviewLens("docs", "other-agent", "advisory", "comment"),
        ),
    )


def test_build_plan_preserves_order_and_required_roles() -> None:
    plan = build_plan(review_policy())

    assert [item.name for item in plan.items] == ["correctness", "docs"]
    assert plan.items[0].required is True
    assert plan.items[1].required is False


def test_json_plan_is_stable_and_contains_no_provider_credentials() -> None:
    plan = build_plan(review_policy())
    document = json.loads(render_plan(plan, "json"))

    assert document == {
        "version": 1,
        "aggregation": "all-required",
        "plan_digest": plan.digest(),
        "lenses": [
            {
                "name": "correctness",
                "preset": "codex",
                "required": True,
                "outcome": "review",
                "publication": "inline",
            },
            {
                "name": "docs",
                "preset": "other-agent",
                "required": False,
                "outcome": "advisory",
                "publication": "comment",
            },
        ],
    }


def test_explicit_bot_overrides_are_bound_into_plan_digest() -> None:
    base_plan = build_plan(
        AgentReviewPolicy(
            version=1,
            required=("correctness",),
            aggregation="all-required",
            lenses=(ReviewLens("correctness", "codex", "review"),),
        )
    )
    custom_plan = build_plan(
        AgentReviewPolicy(
            version=1,
            required=("correctness",),
            aggregation="all-required",
            lenses=(
                ReviewLens(
                    "correctness",
                    "codex",
                    "review",
                    bot_login="custom-reviewer[bot]",
                    bot_user_id=777,
                    pass_reactions=("+1", "rocket"),
                    check_runs=("codex-review",),
                    delay_seconds=10,
                ),
            ),
        )
    )

    assert custom_plan.digest() != base_plan.digest()
    document = json.loads(render_plan(custom_plan, "json"))
    assert document["lenses"][0]["bot_login"] == "custom-reviewer[bot]"
    assert document["lenses"][0]["bot_user_id"] == 777
    assert document["lenses"][0]["pass_reactions"] == ["+1", "rocket"]
    assert document["lenses"][0]["check_runs"] == ["codex-review"]
    assert document["lenses"][0]["delay_seconds"] == 10


def test_text_plan_is_actionable() -> None:
    plan = build_plan(review_policy())
    rendered = render_plan(plan)

    assert "2 lens(es), aggregation all-required" in rendered
    assert "correctness [required, review] via codex" in rendered
    assert "docs [advisory, advisory] via other-agent" in rendered
    assert f"Plan digest: {plan.digest()}" in rendered
