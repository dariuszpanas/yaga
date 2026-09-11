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
            ReviewLens("correctness", "codex", "Review behavior.", "review", "inline"),
            ReviewLens("docs", "other-agent", "Review docs.", "advisory", "comment"),
        ),
    )


def test_build_plan_preserves_order_and_required_roles() -> None:
    plan = build_plan(review_policy())

    assert [item.name for item in plan.items] == ["correctness", "docs"]
    assert plan.items[0].required is True
    assert plan.items[1].required is False


def test_json_plan_is_stable_and_contains_no_provider_credentials() -> None:
    document = json.loads(render_plan(build_plan(review_policy()), "json"))

    assert document == {
        "version": 1,
        "aggregation": "all-required",
        "lenses": [
            {
                "name": "correctness",
                "preset": "codex",
                "instruction": "Review behavior.",
                "required": True,
                "outcome": "review",
                "publication": "inline",
            },
            {
                "name": "docs",
                "preset": "other-agent",
                "instruction": "Review docs.",
                "required": False,
                "outcome": "advisory",
                "publication": "comment",
            },
        ],
    }


def test_text_plan_is_actionable() -> None:
    rendered = render_plan(build_plan(review_policy()))

    assert "2 lens(es), aggregation all-required" in rendered
    assert "correctness [required, review] via codex" in rendered
    assert "docs [advisory, advisory] via other-agent" in rendered


def test_text_plan_sanitizes_instruction_controls() -> None:
    policy = AgentReviewPolicy(
        version=1,
        required=("correctness",),
        aggregation="all-required",
        lenses=(ReviewLens("correctness", "codex", "line\x1b[31m\nnext", "review"),),
    )

    rendered = render_plan(build_plan(policy))

    assert "line?[31m?next" in rendered
    assert "\x1b" not in rendered
