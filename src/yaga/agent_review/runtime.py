"""Provider-neutral entrypoint for the trusted Agent review Action."""

from __future__ import annotations

from yaga.agent_review.github.runtime import operation_name, run_action

__all__ = ["operation_name", "run_action"]
