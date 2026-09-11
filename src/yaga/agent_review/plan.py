"""Provider-neutral execution plans for named Agent review lenses."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

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
            "lenses": [asdict(item) for item in self.items],
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
