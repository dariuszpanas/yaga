"""Resolve bounded Codex review candidates without writing commit status."""

from __future__ import annotations

from yaga.codex.candidates import (
    connector_review_workflow_candidate,
    wake_candidate,
)
from yaga.codex.constants import (
    CODEX_CONNECTOR_LOGIN,
    CODEX_CONNECTOR_USER_ID,
)
from yaga.codex.models import Candidate
from yaga.errors import GateError
from yaga.github import RestApi
from yaga.models import actor_is, positive_int, record


def _candidate_payload(
    candidate: Candidate,
) -> dict[str, object]:
    return candidate.to_payload()


def resolve_event_candidates(
    api: RestApi,
    *,
    repository: str,
    event_name: str,
    event: dict[str, object],
) -> list[dict[str, object]]:
    """Resolve a trusted comment wake-up or stale-pending schedule pass."""
    if event_name == "schedule":
        raise GateError("schedule events require the write-capable boundary repair mode")

    if event_name == "workflow_run":
        candidate = connector_review_workflow_candidate(
            api,
            repository=repository,
            event=event,
        )
        if candidate is None:
            return []
        candidate = wake_candidate(
            api,
            repository=repository,
            pull_request_number=candidate.pull_request_number,
        )
        return [] if candidate is None else [_candidate_payload(candidate)]

    if event_name != "issue_comment":
        raise GateError("candidate resolver event is unsupported")
    issue = record(event.get("issue"), "comment issue")
    comment = record(event.get("comment"), "comment")
    body = comment.get("body")
    connector = actor_is(
        comment,
        user_id=CODEX_CONNECTOR_USER_ID,
        user_login=CODEX_CONNECTOR_LOGIN,
    )
    owner_request = (
        comment.get("author_association") == "OWNER"
        and isinstance(body, str)
        and body == "@codex review"
    )
    if (
        issue.get("pull_request") is None
        or issue.get("state") != "open"
        or not (connector or owner_request)
    ):
        return []
    candidate = wake_candidate(
        api,
        repository=repository,
        pull_request_number=positive_int(issue.get("number"), "comment pull request number"),
    )
    return [] if candidate is None else [_candidate_payload(candidate)]
