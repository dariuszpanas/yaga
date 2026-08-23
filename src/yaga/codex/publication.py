"""Perform fail-closed Codex commit-status publication and race repair."""

from __future__ import annotations

from typing import Any, cast

from yaga.codex.candidates import (
    candidate_from_pull_request,
    open_recovery_candidate,
    same_candidate,
)
from yaga.codex.constants import (
    GITHUB_ACTIONS_LOGIN,
    GITHUB_ACTIONS_USER_ID,
    MAX_OPEN_PULL_REQUESTS,
    STATUS_CONTEXT,
    SUCCESS_CLEANUP_MARGIN_SECONDS,
    SUCCESS_WRITE_REQUEST_RESERVE,
)
from yaga.codex.models import Candidate
from yaga.codex.provenance import (
    ReviewBoundary,
    latest_lifecycle_history,
    newer_boundary,
    newer_ineligible_transition,
)
from yaga.codex.statuses import (
    UNCERTAIN_PENDING_DESCRIPTION,
    boundary_description,
    claimed_actions_boundary,
    latest_status_context_page,
    status_description,
    trusted_actions_status,
    uncertain_pending_status,
    workflow_run_url,
)
from yaga.errors import GateError
from yaga.github import (
    RestApi,
    load_default_branch,
    load_pull_request,
    parse_pull_request,
    require_success_tail,
)
from yaga.models import PullRequest, positive_int, record


def _publish_status(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    state: str,
    description: str | None,
    target_url: str | None,
) -> int:
    request_payload: dict[str, object] = {"state": state, "context": STATUS_CONTEXT}
    if description is not None:
        request_payload["description"] = status_description(description)
    if target_url is not None:
        request_payload["target_url"] = target_url
    payload = record(
        api.post(f"/repos/{repository}/statuses/{head_sha}", request_payload),
        "Codex review status",
    )
    status_id = positive_int(payload.get("id"), "Codex review status ID")
    if (
        payload.get("state") != state
        or payload.get("context") != STATUS_CONTEXT
        or payload.get("description") != description
        or payload.get("target_url") != target_url
    ):
        raise GateError("GitHub returned a different Codex review status")
    creator = record(payload.get("creator"), "Codex review status creator")
    if creator.get("id") != GITHUB_ACTIONS_USER_ID or creator.get("login") != GITHUB_ACTIONS_LOGIN:
        raise GateError("GitHub returned a Codex review status from an unexpected actor")
    return status_id


def publish_pending_boundary(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    boundary: ReviewBoundary,
    target_url: str | None,
) -> int:
    """Publish one already-validated boundary as pending."""
    return _publish_status(
        api,
        repository=repository,
        head_sha=head_sha,
        state="pending",
        description=boundary_description(boundary, state="pending"),
        target_url=target_url,
    )


def publish_uncertain_pending(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    target_url: str,
) -> int:
    """Fail closed when scheduled repair cannot recover a lifecycle source."""
    return _publish_status(
        api,
        repository=repository,
        head_sha=head_sha,
        state="pending",
        description=UNCERTAIN_PENDING_DESCRIPTION,
        target_url=target_url,
    )


def ensure_uncertain_pending(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    candidate: Candidate,
    reconciliation_run_id: int,
) -> PullRequest | None:
    """Persist a fail-closed pending status when no boundary can be proven."""
    pull_request = open_recovery_candidate(api, repository=repository, candidate=candidate)
    if pull_request is None or pull_request.base_ref != load_default_branch(api, repository):
        return None
    probe_error: GateError | None = None
    try:
        latest = latest_status_context_page(
            api,
            repository=repository,
            head_sha=candidate.head_sha,
        )
    except GateError as error:
        latest = None
        probe_error = error
    if uncertain_pending_status(
        latest,
        server_url=server_url,
        repository=repository,
    ):
        return pull_request
    confirmed = open_recovery_candidate(api, repository=repository, candidate=candidate)
    if confirmed is None or confirmed.base_ref != load_default_branch(api, repository):
        return None
    publish_uncertain_pending(
        api,
        repository=repository,
        head_sha=candidate.head_sha,
        target_url=workflow_run_url(server_url, repository, reconciliation_run_id),
    )
    if probe_error is not None:
        raise probe_error
    return confirmed


def _exact_live_default_candidate(
    api: RestApi,
    *,
    repository: str,
    candidate: Candidate,
) -> PullRequest | None:
    """Resolve one exact open candidate, including drafts, on the live default."""
    pull_request = load_pull_request(api, repository, candidate.pull_request_number)
    if pull_request.state != "open" or candidate_from_pull_request(pull_request) != candidate:
        return None
    if pull_request.base_ref != load_default_branch(api, repository):
        return None
    return pull_request


def ensure_live_boundary_pending(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    candidate: Candidate,
    boundary: ReviewBoundary,
) -> PullRequest | None:
    """Persist a source pending for an exact live candidate, including drafts."""
    pull_request = _exact_live_default_candidate(
        api,
        repository=repository,
        candidate=candidate,
    )
    if pull_request is None:
        return None
    boundary_cache: dict[int, ReviewBoundary | None] = {boundary.workflow_run_id: boundary}
    probe_error: GateError | None = None
    try:
        latest = latest_status_context_page(
            api,
            repository=repository,
            head_sha=candidate.head_sha,
        )
    except GateError as error:
        latest = None
        probe_error = error
    if latest is not None:
        trusted = trusted_actions_status(
            api,
            latest,
            server_url=server_url,
            repository=repository,
            head_sha=candidate.head_sha,
            boundary_cache=boundary_cache,
        )
        if trusted == boundary and latest.state == "pending":
            return pull_request
    confirmed = _exact_live_default_candidate(
        api,
        repository=repository,
        candidate=candidate,
    )
    if confirmed is None:
        return None
    publish_pending_boundary(
        api,
        repository=repository,
        head_sha=candidate.head_sha,
        boundary=boundary,
        target_url=workflow_run_url(server_url, repository, boundary.workflow_run_id),
    )
    if probe_error is not None:
        raise probe_error
    return confirmed


def ensure_live_uncertain_pending(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    candidate: Candidate,
    reconciliation_run_id: int,
) -> PullRequest | None:
    """Persist generic pending on an exact live head, including a draft."""
    pull_request = _exact_live_default_candidate(
        api,
        repository=repository,
        candidate=candidate,
    )
    if pull_request is None:
        return None
    probe_error: GateError | None = None
    try:
        latest = latest_status_context_page(
            api,
            repository=repository,
            head_sha=candidate.head_sha,
        )
    except GateError as error:
        latest = None
        probe_error = error
    if uncertain_pending_status(
        latest,
        server_url=server_url,
        repository=repository,
    ):
        return pull_request
    confirmed = _exact_live_default_candidate(
        api,
        repository=repository,
        candidate=candidate,
    )
    if confirmed is None:
        return None
    publish_uncertain_pending(
        api,
        repository=repository,
        head_sha=candidate.head_sha,
        target_url=workflow_run_url(server_url, repository, reconciliation_run_id),
    )
    if probe_error is not None:
        raise probe_error
    return confirmed


def ensure_pending_boundary(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    candidate: Candidate,
    boundary: ReviewBoundary,
    boundary_cache: dict[int, ReviewBoundary | None],
) -> PullRequest | None:
    """Persist a trusted boundary as the physical pending status before ownership."""
    if boundary.head_sha != candidate.head_sha:
        raise GateError("recovery boundary identifies a different candidate head")
    pull_request = open_recovery_candidate(api, repository=repository, candidate=candidate)
    if pull_request is None:
        return None
    if pull_request.base_ref != load_default_branch(api, repository):
        return None

    boundary_cache[boundary.workflow_run_id] = boundary
    probe_error: GateError | None = None
    try:
        latest = latest_status_context_page(
            api,
            repository=repository,
            head_sha=candidate.head_sha,
        )
    except GateError as error:
        latest = None
        probe_error = error
    if latest is not None:
        trusted = trusted_actions_status(
            api,
            latest,
            server_url=server_url,
            repository=repository,
            head_sha=candidate.head_sha,
            boundary_cache=boundary_cache,
        )
        if trusted == boundary and latest.state == "pending":
            return pull_request

    confirmed = open_recovery_candidate(api, repository=repository, candidate=candidate)
    if confirmed is None or confirmed.base_ref != load_default_branch(api, repository):
        return None
    _publish_status(
        api,
        repository=repository,
        head_sha=candidate.head_sha,
        state="pending",
        description=boundary_description(boundary, state="pending"),
        target_url=workflow_run_url(
            server_url,
            repository,
            boundary.workflow_run_id,
        ),
    )
    if probe_error is not None:
        raise probe_error
    return confirmed


def hold_pending_for_lifecycle_history(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    pull_request: PullRequest,
    boundary: ReviewBoundary,
    boundary_cache: dict[int, ReviewBoundary | None],
) -> str | None:
    """Recover a newer or incomplete lifecycle window before trusting success."""
    candidate = candidate_from_pull_request(pull_request)
    try:
        history = latest_lifecycle_history(
            api,
            repository=repository,
            head_sha=pull_request.head_sha,
            not_before=pull_request.created_at,
            pull_request_number=pull_request.number,
        )
    except GateError:
        ensure_pending_boundary(
            api,
            repository=repository,
            server_url=server_url,
            candidate=candidate,
            boundary=boundary,
            boundary_cache=boundary_cache,
        )
        raise
    if history.complete and boundary.workflow_run_id not in history.observed_run_ids:
        ensure_pending_boundary(
            api,
            repository=repository,
            server_url=server_url,
            candidate=candidate,
            boundary=boundary,
            boundary_cache=boundary_cache,
        )
        raise GateError("Codex review lifecycle history omitted the status boundary")
    if newer_ineligible_transition(history, boundary) is not None:
        confirmed = ensure_pending_boundary(
            api,
            repository=repository,
            server_url=server_url,
            candidate=candidate,
            boundary=boundary,
            boundary_cache=boundary_cache,
        )
        return (
            "pending: a newer close or draft transition has no review boundary"
            if confirmed is not None
            else "pending: pull request changed during lifecycle-history validation"
        )

    selected_boundary = history.source.boundary if history.source is not None else None
    if not history.complete:
        if selected_boundary is None and history.newest_source is not None:
            selected_boundary = history.newest_source.boundary
        if selected_boundary is None or newer_boundary(selected_boundary, boundary) == boundary:
            selected_boundary = boundary
    elif selected_boundary is None or newer_boundary(boundary, selected_boundary) == boundary:
        return None

    assert selected_boundary is not None
    confirmed = ensure_pending_boundary(
        api,
        repository=repository,
        server_url=server_url,
        candidate=candidate,
        boundary=selected_boundary,
        boundary_cache=boundary_cache,
    )
    if confirmed is None:
        return "pending: pull request changed during lifecycle-history validation"
    if not history.complete:
        return f"pending for {pull_request.head_sha}: lifecycle history exceeded the bounded window"
    return "pending: a newer lifecycle boundary was recovered"


def _bounded_post_success_ownership(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
) -> tuple[int, PullRequest | None]:
    """Resolve head ownership from one bounded open-PR page after success."""
    payload = api.get(
        f"/repos/{repository}/pulls?state=open&per_page={MAX_OPEN_PULL_REQUESTS + 1}&page=1"
    )
    if (
        not isinstance(payload, list)
        or len(payload) > MAX_OPEN_PULL_REQUESTS
        or not all(isinstance(item, dict) for item in payload)
    ):
        raise GateError("post-success open pull request response is invalid or unbounded")
    pull_requests = [
        parse_pull_request(item, repository=repository)
        for item in cast(list[dict[str, Any]], payload)
    ]
    numbers = [pull_request.number for pull_request in pull_requests]
    if len(set(numbers)) != len(numbers):
        raise GateError("post-success open pull request number is repeated")
    owners = [
        pull_request
        for pull_request in pull_requests
        if pull_request.state == "open" and pull_request.head_sha == head_sha
    ]
    return len(owners), owners[0] if len(owners) == 1 else None


def _post_success_lifecycle_hold(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    pull_request: PullRequest,
    boundary: ReviewBoundary,
    boundary_cache: dict[int, ReviewBoundary | None],
    race_floor: ReviewBoundary | None,
) -> bool:
    """Use one bounded history read to restore lifecycle uncertainty pending."""
    history = latest_lifecycle_history(
        api,
        repository=repository,
        head_sha=pull_request.head_sha,
        not_before=pull_request.created_at,
        pull_request_number=pull_request.number,
    )
    for source in (history.source, history.newest_source):
        if source is not None:
            boundary_cache[source.boundary.workflow_run_id] = source.boundary
    if history.complete and boundary.workflow_run_id not in history.observed_run_ids:
        raise GateError("Codex review lifecycle history omitted the published boundary")
    if newer_ineligible_transition(history, boundary) is not None:
        publish_pending_boundary(
            api,
            repository=repository,
            head_sha=pull_request.head_sha,
            boundary=boundary,
            target_url=workflow_run_url(server_url, repository, boundary.workflow_run_id),
        )
        return True
    selected = history.source.boundary if history.source is not None else None
    if history.newest_source is not None:
        selected = (
            history.newest_source.boundary
            if selected is None
            else newer_boundary(selected, history.newest_source.boundary)
        )
    if not history.complete:
        selected = boundary if selected is None else newer_boundary(boundary, selected)
        publish_pending_boundary(
            api,
            repository=repository,
            head_sha=pull_request.head_sha,
            boundary=selected,
            target_url=workflow_run_url(server_url, repository, selected.workflow_run_id),
        )
        return True
    comparison = race_floor or boundary
    if selected is not None and newer_boundary(comparison, selected) != comparison:
        publish_pending_boundary(
            api,
            repository=repository,
            head_sha=pull_request.head_sha,
            boundary=selected,
            target_url=workflow_run_url(server_url, repository, selected.workflow_run_id),
        )
        return True
    return False


def _post_success_status_hold(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    head_sha: str,
    success_status_id: int,
    boundary: ReviewBoundary,
    target_url: str | None,
    boundary_cache: dict[int, ReviewBoundary | None],
    race_floor: ReviewBoundary | None,
) -> bool:
    """Inspect one physical status page and repair any post-success race."""
    latest = latest_status_context_page(api, repository=repository, head_sha=head_sha)
    if latest is None:
        raise GateError("published Codex success is absent from the latest status page")
    if latest.status_id == success_status_id and latest.state == "success":
        return False
    if latest.state == "pending":
        return True
    claim = claimed_actions_boundary(
        latest,
        server_url=server_url,
        repository=repository,
        head_sha=head_sha,
    )
    trusted: ReviewBoundary | None = None
    if claim is not None:
        trusted = trusted_actions_status(
            api,
            latest,
            server_url=server_url,
            repository=repository,
            head_sha=head_sha,
            boundary_cache=boundary_cache,
            source_validation_limit=len(boundary_cache) + 1,
        )
    if trusted == boundary and latest.state == "success":
        return False
    comparison = race_floor or boundary
    restore = (
        trusted
        if trusted is not None and newer_boundary(comparison, trusted) != comparison
        else boundary
    )
    publish_pending_boundary(
        api,
        repository=repository,
        head_sha=head_sha,
        boundary=restore,
        target_url=(
            latest.target_url
            if restore == trusted and latest.target_url is not None
            else target_url
        ),
    )
    return True


def publish_success_with_pending_repair(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    pull_request: PullRequest,
    boundary: ReviewBoundary,
    target_url: str | None,
    boundary_cache: dict[int, ReviewBoundary | None],
    race_floor: ReviewBoundary | None = None,
) -> bool:
    """Publish success, then repair lifecycle, ownership, or status races."""
    head_sha = pull_request.head_sha
    require_success_tail(
        api,
        SUCCESS_WRITE_REQUEST_RESERVE,
        cleanup_margin_seconds=SUCCESS_CLEANUP_MARGIN_SECONDS,
    )
    try:
        success_status_id = _publish_status(
            api,
            repository=repository,
            head_sha=head_sha,
            state="success",
            description=boundary_description(boundary, state="success"),
            target_url=target_url,
        )

        open_count, owner = _bounded_post_success_ownership(
            api,
            repository=repository,
            head_sha=head_sha,
        )
        if open_count == 0:
            # A merge or close can complete while the success response is in
            # flight.  Do not leave a fresh pending status on a completed PR.
            return False
        if not same_candidate(pull_request, owner) or pull_request.base_ref != load_default_branch(
            api, repository
        ):
            _publish_status(
                api,
                repository=repository,
                head_sha=head_sha,
                state="pending",
                description=boundary_description(boundary, state="pending"),
                target_url=target_url,
            )
            return True

        if _post_success_lifecycle_hold(
            api,
            repository=repository,
            server_url=server_url,
            pull_request=pull_request,
            boundary=boundary,
            boundary_cache=boundary_cache,
            race_floor=race_floor,
        ):
            return True
        return _post_success_status_hold(
            api,
            repository=repository,
            server_url=server_url,
            head_sha=head_sha,
            success_status_id=success_status_id,
            boundary=boundary,
            target_url=target_url,
            boundary_cache=boundary_cache,
            race_floor=race_floor,
        )
    except GateError as error:
        try:
            _publish_status(
                api,
                repository=repository,
                head_sha=head_sha,
                state="pending",
                description=boundary_description(boundary, state="pending"),
                target_url=target_url,
            )
        except GateError as repair_error:
            raise GateError(
                f"{error}; fail-closed pending repair also failed: {repair_error}"
            ) from error
        raise
