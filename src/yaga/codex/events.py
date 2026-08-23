"""Parse trusted pull-request lifecycle events for required-check invalidation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from yaga.codex.constants import EVENT_ACTIONS
from yaga.errors import GateError
from yaga.github import parse_pull_request
from yaga.models import (
    PullRequest,
    commit_sha,
    login,
    positive_int,
    record,
    ref_name,
    repository_name,
    timestamp,
)

EventDisposition = Literal["closed", "draft", "noop", "review"]


@dataclass(frozen=True)
class EventBoundary:
    """Local fields sufficient to invalidate inherited status success."""

    action: str
    disposition: EventDisposition
    repository: str
    default_branch: str
    pull_request_number: int
    head_sha: str | None
    base_sha: str | None
    base_ref: str | None
    occurred_at: datetime | None
    author_id: int | None
    author_login: str | None


@dataclass(frozen=True)
class CodexEvent:
    """A fully parsed candidate validated only after pending exists."""

    action: str
    disposition: EventDisposition
    default_branch: str
    pull_request_number: int
    pull_request: PullRequest | None
    occurred_at: datetime | None


def _base_changed(event: dict[str, object]) -> bool:
    changes_value = event.get("changes")
    if changes_value is None:
        return False
    changes = record(changes_value, "pull request event changes")
    base_value = changes.get("base")
    if base_value is None:
        return False
    record(base_value, "pull request base change")
    return True


def parse_event_boundary(
    *,
    repository: str,
    event_name: str,
    event: dict[str, object],
) -> EventBoundary:
    """Extract bounded local event fields before live-state API validation."""
    repository = repository_name(repository)
    if event_name != "pull_request_target":
        raise GateError("Codex review requires a pull_request_target event")
    event_repository = record(event.get("repository"), "GitHub event repository")
    if event_repository.get("full_name") != repository:
        raise GateError("GitHub event belongs to another repository")
    default_branch = ref_name(event_repository.get("default_branch"), "default branch")
    action = event.get("action")
    if action not in EVENT_ACTIONS:
        raise GateError("pull request action is invalid")
    action = cast(str, action)
    payload = record(event.get("pull_request"), "GitHub event pull request")
    number = positive_int(payload.get("number"), "pull request number")
    if positive_int(event.get("number"), "pull request event number") != number:
        raise GateError("pull request event identifies a different pull request")
    state = payload.get("state")
    draft = payload.get("draft")
    if state not in {"closed", "open"} or not isinstance(draft, bool):
        raise GateError("pull request event state is invalid")
    if action == "closed":
        if state != "closed":
            raise GateError("closed pull request event has an invalid state")
        return EventBoundary(
            action,
            "closed",
            repository,
            default_branch,
            number,
            None,
            None,
            None,
            None,
            None,
            None,
        )
    if state != "open":
        raise GateError("active pull request event is not open")

    head = record(payload.get("head"), "pull request event head")
    base = record(payload.get("base"), "pull request event base")
    base_repository = record(base.get("repo"), "pull request event base repository")
    if repository_name(base_repository.get("full_name")) != repository:
        raise GateError("pull request event base repository is invalid")
    author = record(payload.get("user"), "pull request event author")
    head_sha = commit_sha(head.get("sha"), "pull request event head")
    base_sha = commit_sha(base.get("sha"), "pull request event base")
    base_ref = ref_name(base.get("ref"), "pull request event base ref")
    occurred_at = timestamp(payload.get("updated_at"), "pull request event boundary")
    author_id = positive_int(author.get("id"), "pull request event author ID")
    author_login = login(author.get("login"), "pull request event author login")

    if action == "ready_for_review" and draft:
        raise GateError("ready transition still identifies a draft pull request")
    if action == "converted_to_draft" and not draft:
        raise GateError("draft transition does not identify a draft pull request")
    if action == "edited" and not _base_changed(event):
        disposition: EventDisposition = "noop"
    elif action == "converted_to_draft" or draft:
        disposition = "draft"
    else:
        disposition = "review"
    return EventBoundary(
        action,
        disposition,
        repository,
        default_branch,
        number,
        head_sha,
        base_sha,
        base_ref,
        occurred_at,
        author_id,
        author_login,
    )


def parse_codex_event(
    *,
    repository: str,
    event_name: str,
    event: dict[str, object],
) -> CodexEvent:
    """Fully validate the event before lifecycle statuses are invalidated."""
    boundary = parse_event_boundary(
        repository=repository,
        event_name=event_name,
        event=event,
    )
    if boundary.disposition == "closed":
        return CodexEvent(
            boundary.action,
            boundary.disposition,
            boundary.default_branch,
            boundary.pull_request_number,
            None,
            None,
        )
    payload = record(event.get("pull_request"), "GitHub event pull request")
    pull_request = parse_pull_request(
        payload,
        repository=boundary.repository,
        expected_number=boundary.pull_request_number,
    )
    if (
        pull_request.head_sha != boundary.head_sha
        or pull_request.base_sha != boundary.base_sha
        or pull_request.base_ref != boundary.base_ref
        or pull_request.state != "open"
    ):
        raise GateError("full pull request event disagrees with its local boundary")
    if boundary.disposition == "draft" and not pull_request.draft:
        raise GateError("draft event does not identify a draft pull request")
    if boundary.disposition == "review" and pull_request.draft:
        raise GateError("review event still identifies a draft pull request")
    return CodexEvent(
        boundary.action,
        boundary.disposition,
        boundary.default_branch,
        boundary.pull_request_number,
        pull_request,
        boundary.occurred_at,
    )
