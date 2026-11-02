"""Provider-neutral execution plans for named Agent review lenses."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from yaga.agent_review.policy import AgentReviewPolicy
from yaga.errors import safe_error_text


@dataclass(frozen=True, slots=True)
class ReviewPlanItem:
    """One deterministic adapter invocation derived from policy."""

    name: str
    preset: str
    instruction: str
    required: bool
    outcome: str
    publication: str
    bot_login: str | None = None
    bot_user_id: int | None = None
    bot_app_id: int | None = None
    pass_reactions: tuple[str, ...] | None = None
    pending_reactions: tuple[str, ...] | None = None


def _item_document(item: ReviewPlanItem) -> dict[str, object]:
    document: dict[str, object] = {
        "name": item.name,
        "preset": item.preset,
        "instruction": item.instruction,
        "required": item.required,
        "outcome": item.outcome,
        "publication": item.publication,
    }
    if item.bot_login is not None:
        document["bot_login"] = item.bot_login
    if item.bot_user_id is not None:
        document["bot_user_id"] = item.bot_user_id
    if item.bot_app_id is not None:
        document["bot_app_id"] = item.bot_app_id
    if item.pass_reactions is not None:
        document["pass_reactions"] = list(item.pass_reactions)
    if item.pending_reactions is not None:
        document["pending_reactions"] = list(item.pending_reactions)
    return document


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    """Provider-neutral plan that adapters can execute independently."""

    version: int
    aggregation: str
    items: tuple[ReviewPlanItem, ...]

    def digest(self) -> str:
        """Return the deterministic identity of this exact provider-neutral plan."""
        canonical = json.dumps(
            self._base_document(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _base_document(self) -> dict[str, object]:
        return {
            "version": self.version,
            "aggregation": self.aggregation,
            "lenses": [_item_document(item) for item in self.items],
        }

    def document(self) -> dict[str, object]:
        """Return the stable, provider-neutral JSON document."""
        return self._base_document() | {"plan_digest": self.digest()}


def build_plan(policy: AgentReviewPolicy) -> ReviewPlan:
    """Expand one validated policy into ordered adapter work items."""
    if not isinstance(policy, AgentReviewPolicy):
        raise TypeError("policy must be an AgentReviewPolicy")
    required = set(policy.required)
    return ReviewPlan(
        version=policy.version,
        aggregation=policy.aggregation,
        items=tuple(
            ReviewPlanItem(
                name=lens.name,
                preset=lens.preset,
                instruction=lens.instruction,
                required=lens.name in required,
                outcome=lens.outcome,
                publication=lens.publication,
                bot_login=lens.bot_login,
                bot_user_id=lens.bot_user_id,
                bot_app_id=lens.bot_app_id,
                pass_reactions=lens.pass_reactions,
                pending_reactions=lens.pending_reactions,
            )
            for lens in policy.lenses
        ),
    )


def render_plan(plan: ReviewPlan, output_format: str = "text") -> str:
    """Render a plan for humans or provider-neutral automation."""
    if not isinstance(plan, ReviewPlan):
        raise TypeError("plan must be a ReviewPlan")
    if output_format == "json":
        return json.dumps(plan.document(), ensure_ascii=False, indent=2)
    if output_format != "text":
        raise ValueError("output format must be text or json")
    lines = [f"Agent review plan: {len(plan.items)} lens(es), aggregation {plan.aggregation}."]
    lines.append(f"Plan digest: {plan.digest()}")
    for item in plan.items:
        role = "required" if item.required else "advisory"
        lines.append(f"- {item.name} [{role}, {item.outcome}] via {item.preset}")
        lines.append(f"  {safe_error_text(item.instruction, maximum=4096)}")
    return "\n".join(lines)


def render_policy_check(policy: AgentReviewPolicy, output_format: str = "text") -> str:
    """Render a successful policy validation for humans or automation."""
    if not isinstance(policy, AgentReviewPolicy):
        raise TypeError("policy must be an AgentReviewPolicy")
    if output_format == "json":
        plan = build_plan(policy)
        document = {
            "version": policy.version,
            "status": "passed",
            "aggregation": policy.aggregation,
            "lens_count": len(policy.lenses),
            "required_count": len(policy.required),
            "required": list(policy.required),
            "plan_digest": plan.digest(),
        }
        return json.dumps(document, ensure_ascii=False, indent=2)
    if output_format != "text":
        raise ValueError("output format must be text or json")
    required = ", ".join(policy.required)
    return (
        f"Agent review policy passed: {len(policy.lenses)} lens(es), "
        f"{len(policy.required)} required ({required}), aggregation {policy.aggregation}."
    )
