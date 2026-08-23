"""Run the bounded, write-capable scheduled Codex boundary repair pass."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from yaga.codex.candidates import candidate_from_pull_request, same_candidate
from yaga.codex.constants import (
    MAX_OPEN_PULL_REQUESTS,
    MAX_SCHEDULE_CANDIDATES,
    SCHEDULE_INTERVAL_MINUTES,
)
from yaga.codex.models import Candidate
from yaga.codex.provenance import (
    LifecycleSource,
    latest_lifecycle_history,
    newer_ineligible_transition,
)
from yaga.codex.publication import publish_pending_boundary, publish_uncertain_pending
from yaga.codex.statuses import (
    claimed_actions_boundary,
    latest_status_context_page,
    uncertain_pending_status,
    workflow_run_url,
)
from yaga.errors import GateError
from yaga.github import RestApi, load_default_branch, parse_pull_request
from yaga.models import (
    PullRequest,
    commit_sha,
    positive_int,
    record,
    ref_name,
    repository_name,
    timestamp,
)
from yaga.status import CommitStatus


@dataclass(frozen=True)
class _PendingIntent:
    """One live-list candidate that must be fail-closed before terminal work."""

    pull_request: _ScheduledPullRequest
    source: LifecycleSource | None


@dataclass(frozen=True)
class _ScheduledPullRequest:
    """The minimum safe repair identity plus an optional full candidate."""

    number: int
    head_sha: str
    base_ref: str
    draft: bool
    state: str
    created_at: datetime
    candidate: PullRequest | None


def _scheduled_pull_request(value: object, *, repository: str) -> _ScheduledPullRequest:
    """Salvage a fail-closed repair identity from a candidate-local malformed record."""
    payload = record(value, "scheduled pull request")
    number = positive_int(payload.get("number"), "scheduled pull request number")
    head = record(payload.get("head"), "scheduled pull request head")
    base = record(payload.get("base"), "scheduled pull request base")
    base_repository = record(base.get("repo"), "scheduled pull request base repository")
    if repository_name(base_repository.get("full_name")) != repository:
        raise GateError("scheduled pull request base repository is invalid")
    draft = payload.get("draft")
    state = payload.get("state")
    if not isinstance(draft, bool) or state not in {"open", "closed"}:
        raise GateError("scheduled pull request state is invalid")
    try:
        candidate = parse_pull_request(payload, repository=repository)
    except GateError:
        candidate = None
    return _ScheduledPullRequest(
        number=number,
        head_sha=commit_sha(head.get("sha"), "scheduled pull request head"),
        base_ref=ref_name(base.get("ref"), "scheduled pull request base ref"),
        draft=draft,
        state=state,
        created_at=timestamp(payload.get("created_at"), "scheduled pull request creation"),
        candidate=candidate,
    )


def _status_matches_source(
    status: CommitStatus | None,
    source: LifecycleSource,
    *,
    server_url: str,
    repository: str,
    head_sha: str,
) -> bool:
    if status is None:
        return False
    claim = claimed_actions_boundary(
        status,
        server_url=server_url,
        repository=repository,
        head_sha=head_sha,
    )
    boundary = source.boundary
    return claim is not None and (
        claim.pull_request_number,
        claim.head_sha,
        claim.base_sha,
        claim.occurred_at,
        claim.workflow_run_id,
    ) == (
        boundary.pull_request_number,
        boundary.head_sha,
        boundary.base_sha,
        boundary.occurred_at,
        boundary.workflow_run_id,
    )


def _source_matches_candidate(source: LifecycleSource, pull_request: PullRequest) -> bool:
    boundary = source.boundary
    return (
        boundary.pull_request_number == pull_request.number
        and boundary.head_sha == pull_request.head_sha
        and boundary.base_sha == pull_request.base_sha
        and boundary.base_ref == pull_request.base_ref
    )


def _publish_pending_intents(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    initial_default_branch: str,
    repair_target_url: str,
    intents: list[_PendingIntent],
) -> None:
    """Revalidate pending intents immediately before their bounded write phase."""
    if not intents:
        return
    current_default_branch = load_default_branch(api, repository)
    errors: list[str] = []
    for intent in intents:
        listed = intent.pull_request
        try:
            live = _scheduled_pull_request(
                api.get(f"/repos/{repository}/pulls/{listed.number}"),
                repository=repository,
            )
            if live.state != "open" or live.draft or live.base_ref != current_default_branch:
                continue
            if live.head_sha != listed.head_sha:
                continue
            if (
                intent.source is not None
                and current_default_branch == initial_default_branch
                and listed.candidate is not None
                and live.candidate is not None
                and same_candidate(listed.candidate, live.candidate)
            ):
                publish_pending_boundary(
                    api,
                    repository=repository,
                    head_sha=live.head_sha,
                    boundary=intent.source.boundary,
                    target_url=workflow_run_url(
                        server_url,
                        repository,
                        intent.source.boundary.workflow_run_id,
                    ),
                )
            else:
                publish_uncertain_pending(
                    api,
                    repository=repository,
                    head_sha=live.head_sha,
                    target_url=repair_target_url,
                )
        except GateError as error:
            errors.append(f"PR #{listed.number}: {error}")
    if errors:
        raise GateError("scheduled pending repair failed: " + "; ".join(errors))


def _rotating_window(candidates: list[Candidate], *, now: datetime) -> list[Candidate]:
    if len(candidates) <= MAX_SCHEDULE_CANDIDATES:
        return candidates
    slot_seconds = SCHEDULE_INTERVAL_MINUTES * 60
    slot = int(now.timestamp()) // slot_seconds
    start = (slot * MAX_SCHEDULE_CANDIDATES) % len(candidates)
    return [
        candidates[(start + offset) % len(candidates)] for offset in range(MAX_SCHEDULE_CANDIDATES)
    ]


def repair_scheduled_boundaries(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    repair_run_id: int,
    now: datetime | None = None,
) -> list[Candidate]:
    """Repair every detected lost boundary, then select bounded terminal work."""
    repository = repository_name(repository)
    repair_target_url = workflow_run_url(server_url, repository, repair_run_id)
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None or now.utcoffset() is None:
        raise GateError("scheduled repair time must include a timezone")
    else:
        now = now.astimezone(UTC)

    default_branch = load_default_branch(api, repository)
    payload = api.get(
        f"/repos/{repository}/pulls?state=open&per_page={MAX_OPEN_PULL_REQUESTS + 1}&page=1"
    )
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise GateError("open pull request response is invalid")
    if len(payload) > MAX_OPEN_PULL_REQUESTS:
        raise GateError("too many open pull requests for bounded Codex repair")

    pull_requests: list[_ScheduledPullRequest] = []
    parse_errors: list[str] = []
    seen_numbers: set[int] = set()
    for index, item in enumerate(cast(list[dict[str, Any]], payload)):
        try:
            pull_request = _scheduled_pull_request(item, repository=repository)
        except GateError as error:
            parse_errors.append(f"record {index}: {error}")
            continue
        if pull_request.number in seen_numbers:
            parse_errors.append(f"record {index}: open pull request number is repeated")
            continue
        seen_numbers.add(pull_request.number)
        pull_requests.append(pull_request)
    open_head_counts = Counter(
        pull_request.head_sha for pull_request in pull_requests if pull_request.state == "open"
    )

    terminal_candidates: list[Candidate] = []
    pending_intents: list[_PendingIntent] = []
    for pull_request in sorted(pull_requests, key=lambda item: item.number):
        if (
            pull_request.state != "open"
            or pull_request.draft
            or pull_request.base_ref != default_branch
        ):
            continue
        if pull_request.candidate is None:
            pending_intents.append(_PendingIntent(pull_request, None))
            continue
        try:
            history = latest_lifecycle_history(
                api,
                repository=repository,
                head_sha=pull_request.head_sha,
                not_before=pull_request.created_at,
                pull_request_number=pull_request.number,
            )
        except GateError:
            pending_intents.append(_PendingIntent(pull_request, None))
            continue
        source = history.source or (None if history.complete else history.newest_source)
        transition = newer_ineligible_transition(
            history,
            history.source.boundary if history.source is not None else None,
        )
        if transition is not None:
            source = None
        try:
            latest = latest_status_context_page(
                api,
                repository=repository,
                head_sha=pull_request.head_sha,
            )
        except GateError:
            pending_intents.append(_PendingIntent(pull_request, source))
            continue
        if source is None:
            if not uncertain_pending_status(
                latest,
                server_url=server_url,
                repository=repository,
            ):
                pending_intents.append(_PendingIntent(pull_request, None))
            continue
        if source.boundary.occurred_at > now:
            pending_intents.append(_PendingIntent(pull_request, None))
            continue
        represented = _status_matches_source(
            latest,
            source,
            server_url=server_url,
            repository=repository,
            head_sha=pull_request.head_sha,
        )
        current_identity = _source_matches_candidate(source, pull_request.candidate)
        if represented and latest is not None and latest.state == "pending":
            if (
                history.complete
                and current_identity
                and open_head_counts[pull_request.head_sha] == 1
            ):
                terminal_candidates.append(candidate_from_pull_request(pull_request.candidate))
            continue
        if (
            represented
            and latest is not None
            and latest.state == "success"
            and history.complete
            and current_identity
            and open_head_counts[pull_request.head_sha] == 1
        ):
            continue

        pending_intents.append(_PendingIntent(pull_request, source))

    _publish_pending_intents(
        api,
        repository=repository,
        server_url=server_url,
        initial_default_branch=default_branch,
        repair_target_url=repair_target_url,
        intents=pending_intents,
    )
    if parse_errors:
        raise GateError("scheduled pull request repair failed: " + "; ".join(parse_errors))

    return _rotating_window(terminal_candidates, now=now)
