"""Orchestrate authoritative exact-candidate Codex review status publication.

The trusted default-branch workflow records lifecycle boundaries as pending
commit statuses. This module settles only those pending candidates from exact
Codex connector outcomes. A completed candidate is durable: later edits or
deletions of its displayed outcome do not create duplicate model work.
"""

from __future__ import annotations

from typing import Any

from yaga.codex.candidates import (
    boundary_is_displaced,
    event_lifecycle_candidate,
    head_ownership,
    open_candidate,
    same_candidate,
)
from yaga.codex.constants import (
    BOUNDARY_ACTIONS,
)
from yaga.codex.models import Candidate
from yaga.codex.provenance import (
    ReviewBoundary,
    lifecycle_source,
)
from yaga.codex.publication import (
    ensure_live_boundary_pending,
    ensure_pending_boundary,
)
from yaga.codex.reconciliation import reconcile_exact_candidate
from yaga.codex.statuses import (
    authoritative_status,
    boundary_description,
    status_context_usage,
)
from yaga.errors import GateError
from yaga.github import RestApi
from yaga.models import repository_name


def invalidate_lifecycle_event(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    event_name: str,
    event: dict[str, object],
) -> Candidate | None:
    """Persist one fail-closed boundary before head-serialized reconciliation.

    The observer completion is trusted only as repository-owned metadata. The
    live pull request, default base branch, and source workflow run are all
    re-read before a status can be written. Pending is persisted before unique
    head ownership is required, so a shared head can never inherit success.
    Reruns keep an already-pending exact boundary idempotent.
    """
    repository = repository_name(repository)
    if event_name != "workflow_run":
        raise GateError("boundary invalidation requires an observer completion")
    candidate, source = event_lifecycle_candidate(api, repository=repository, event=event)
    if (
        candidate is None
        or source.action not in BOUNDARY_ACTIONS | {"converted_to_draft"}
        or (source.action == "edited") != source.base_changed
    ):
        return None
    if source.action == "converted_to_draft":
        ensure_live_boundary_pending(
            api,
            repository=repository,
            server_url=server_url,
            candidate=candidate,
            boundary=source.boundary,
        )
        return None
    pull_request = open_candidate(
        api,
        repository=repository,
        pull_request_number=candidate.pull_request_number,
        expected_head=candidate.head_sha,
        expected_base_sha=candidate.base_sha,
        expected_base_ref=candidate.base_ref,
        expected_head_repository=candidate.head_repository,
        expected_head_ref=candidate.head_ref,
    )
    if pull_request is None:
        return None

    boundary_cache: dict[int, ReviewBoundary | None] = {
        source.boundary.workflow_run_id: source.boundary
    }
    confirmed = ensure_pending_boundary(
        api,
        repository=repository,
        server_url=server_url,
        candidate=candidate,
        boundary=source.boundary,
        boundary_cache=boundary_cache,
    )
    if confirmed is None:
        return None

    # Pending is now physically latest. Historical selection may fail or hit a
    # validation budget, but it can no longer leave an older success green.
    usage = status_context_usage(api, repository=repository, head_sha=candidate.head_sha)
    head_authoritative = authoritative_status(
        api,
        usage,
        server_url=server_url,
        repository=repository,
        pull_request_number=None,
        head_sha=candidate.head_sha,
        base_sha=None,
        base_ref=None,
        boundary_cache=boundary_cache,
    )
    selected_source = source
    if head_authoritative is not None:
        _, head_boundary = head_authoritative
        if (head_boundary.occurred_at, head_boundary.workflow_run_id) > (
            source.boundary.occurred_at,
            source.boundary.workflow_run_id,
        ) and not boundary_is_displaced(
            api,
            repository=repository,
            head_sha=candidate.head_sha,
            current_pull_request=pull_request,
            boundary=head_boundary,
        ):
            selected_source = lifecycle_source(
                api,
                repository=repository,
                run_id=head_boundary.workflow_run_id,
            )
            confirmed = ensure_pending_boundary(
                api,
                repository=repository,
                server_url=server_url,
                candidate=candidate,
                boundary=selected_source.boundary,
                boundary_cache=boundary_cache,
            )
            if confirmed is None:
                return None
    if selected_source.boundary != source.boundary:
        return None
    ownership = head_ownership(api, repository=repository, head_sha=candidate.head_sha)
    return candidate if same_candidate(confirmed, ownership.pull_request) else None


def reconcile_candidate(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    candidate: Candidate,
    reconciliation_run_id: int,
) -> str:
    """Revalidate and reconcile one exact, head-serialized candidate."""

    return reconcile_exact_candidate(
        api,
        repository=repository,
        server_url=server_url,
        expected_pull_request=candidate.pull_request_number,
        expected_head=candidate.head_sha,
        expected_base_sha=candidate.base_sha,
        expected_base_ref=candidate.base_ref,
        expected_head_repository=candidate.head_repository,
        expected_head_ref=candidate.head_ref,
        reconciliation_run_id=reconciliation_run_id,
    )


def reconcile_lifecycle_event(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    event_name: str,
    event: dict[str, Any],
    expected_candidate: Candidate | None = None,
    reconciliation_run_id: int,
) -> str:
    """Handle one trusted observer completion without creating a review request."""
    repository = repository_name(repository)
    if event_name != "workflow_run":
        raise GateError("lifecycle mode requires an observer completion")
    candidate, source = event_lifecycle_candidate(api, repository=repository, event=event)
    if source.action in {"closed", "converted_to_draft"}:
        return "skipped: observer event is not a Codex review boundary"
    if candidate is None:
        return "skipped: observer candidate changed before lifecycle reconciliation"
    if expected_candidate is not None and candidate != expected_candidate:
        raise GateError("lifecycle candidate does not match the invalidated boundary")
    if source.action not in BOUNDARY_ACTIONS or (source.action == "edited") != source.base_changed:
        raise GateError("observer event is not a review boundary")
    boundary_description(source.boundary, state="pending")
    return reconcile_candidate(
        api,
        repository=repository,
        server_url=server_url,
        candidate=candidate,
        reconciliation_run_id=reconciliation_run_id,
    )
