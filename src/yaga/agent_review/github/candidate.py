"""Resolve one live PR and its authenticated YAGA lifecycle boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from yaga.agent_review.github.boundary import (
    ReviewBoundary,
    parse_boundary_description,
    same_boundary,
    validate_boundary_source,
)
from yaga.agent_review.github.constants import (
    CODEX_STATUS_CONTEXT,
    GITHUB_ACTIONS_LOGIN,
    GITHUB_ACTIONS_USER_ID,
    MAX_HEAD_ASSOCIATIONS,
)
from yaga.agent_review.github.publication import status_history
from yaga.agent_review.github.runs import SourceRun
from yaga.errors import GateError
from yaga.github import RestApi, load_default_branch, parse_pull_request
from yaga.models import PullRequest, login, positive_int, record, repository_name
from yaga.status import CommitStatus


@dataclass(frozen=True)
class LiveCandidate:
    """Exact ready PR, immutable author identity, and lifecycle capability."""

    pull_request: PullRequest
    author_id: int
    author_login: str
    default_branch: str
    boundary: ReviewBoundary
    lifecycle_updated_at: datetime
    latest_codex_status: CommitStatus


def _unique_open_number(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
) -> int | None:
    payload = api.get(
        f"/repos/{repository}/commits/{head_sha}/pulls?per_page={MAX_HEAD_ASSOCIATIONS}&page=1"
    )
    if not isinstance(payload, list) or len(payload) > MAX_HEAD_ASSOCIATIONS:
        raise GateError("source head pull-request associations are invalid")
    if len(payload) == MAX_HEAD_ASSOCIATIONS:
        raise GateError("source head pull-request associations are truncated")
    seen: set[int] = set()
    open_numbers: list[int] = []
    for index, value in enumerate(payload):
        item = record(value, f"source head association {index}")
        number = positive_int(item.get("number"), f"source head association {index} number")
        if number in seen:
            raise GateError("source head association is repeated")
        seen.add(number)
        state = item.get("state")
        if state not in {"closed", "open"}:
            raise GateError("source head association state is invalid")
        head = record(item.get("head"), f"source head association {index} head")
        if state == "open" and head.get("sha") == head_sha:
            open_numbers.append(number)
    if not open_numbers:
        return None
    if len(open_numbers) != 1:
        raise GateError("source head has ambiguous open pull-request ownership")
    return open_numbers[0]


def _phase_matches_state(status: CommitStatus, phase: str) -> bool:
    return (status.state, phase) in {
        ("pending", "CI pending"),
        ("pending", "Codex pending"),
        ("success", "Codex passed"),
        ("error", "Codex failed"),
    }


def _boundary_from_statuses(
    api: RestApi,
    *,
    repository: str,
    lifecycle_workflow: str,
    server_url: str,
    pull_request: PullRequest,
) -> tuple[ReviewBoundary, datetime, CommitStatus]:
    statuses = status_history(
        api,
        repository=repository,
        head_sha=pull_request.head_sha,
        context=CODEX_STATUS_CONTEXT,
    )
    if not statuses:
        raise GateError("Agent Review has no lifecycle boundary")
    latest = statuses[0]
    if latest.creator_id != GITHUB_ACTIONS_USER_ID or latest.creator_login != GITHUB_ACTIONS_LOGIN:
        raise GateError("latest Agent Review status is not Actions-owned")
    claim = parse_boundary_description(latest, head_sha=pull_request.head_sha)
    if claim is None or not _phase_matches_state(latest, claim[1]):
        raise GateError("latest Agent Review status has no valid lifecycle boundary")
    claimed_boundary = claim[0]
    if (
        claimed_boundary.pull_request_number != pull_request.number
        or claimed_boundary.base_sha != pull_request.base_sha
        or claimed_boundary.action in {"closed", "converted_to_draft"}
    ):
        raise GateError("Agent Review lifecycle boundary is not the live candidate")

    provenance: tuple[ReviewBoundary, CommitStatus] | None = None
    for status in statuses:
        parsed = parse_boundary_description(status, head_sha=pull_request.head_sha)
        if parsed is None or parsed[1] != "CI pending":
            continue
        boundary = parsed[0]
        if same_boundary(boundary, claimed_boundary):
            provenance = boundary, status
            break
    if provenance is None:
        raise GateError("Agent Review lifecycle provenance is outside the bounded history")
    boundary, provenance_status = provenance
    lifecycle_updated_at = validate_boundary_source(
        api,
        repository=repository,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
        boundary=boundary,
        provenance_status=provenance_status,
    )
    return boundary, lifecycle_updated_at, latest


def load_live_candidate(
    api: RestApi,
    *,
    repository: str,
    source: SourceRun,
    lifecycle_workflow: str,
    server_url: str,
) -> LiveCandidate | None:
    """Load the unique ready default-branch PR for one exact CI source run."""
    repository = repository_name(repository)
    number = _unique_open_number(
        api,
        repository=repository,
        head_sha=source.head_sha,
    )
    if number is None:
        return None
    if number != source.pull_request_number:
        raise GateError("source CI belongs to a different pull request")
    payload = record(api.get(f"/repos/{repository}/pulls/{number}"), "live pull request")
    pull_request = parse_pull_request(
        payload,
        repository=repository,
        expected_number=number,
    )
    author = record(payload.get("user"), "live pull request author")
    author_id = positive_int(author.get("id"), "live pull request author ID")
    author_login = login(author.get("login"), "live pull request author login")
    default_branch = load_default_branch(api, repository)
    if (
        pull_request.state != "open"
        or pull_request.draft
        or pull_request.head_sha != source.head_sha
        or pull_request.base_ref != default_branch
        or pull_request.base_sha != source.base_sha
        or pull_request.head_ref != source.head_ref
        or pull_request.head_repository != source.head_repository
    ):
        return None
    boundary, lifecycle_updated_at, latest = _boundary_from_statuses(
        api,
        repository=repository,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
        pull_request=pull_request,
    )
    if source.pull_request_action != boundary.action or source.created_at < boundary.occurred_at:
        raise GateError("source CI was not created for the current lifecycle boundary")
    return LiveCandidate(
        pull_request=pull_request,
        author_id=author_id,
        author_login=author_login,
        default_branch=default_branch,
        boundary=boundary,
        lifecycle_updated_at=lifecycle_updated_at,
        latest_codex_status=latest,
    )
