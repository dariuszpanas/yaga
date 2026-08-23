"""Reconcile one exact pending Codex candidate to a durable terminal result."""

from __future__ import annotations

from dataclasses import dataclass

from yaga.codex.candidates import (
    boundary_is_displaced,
    candidate_from_pull_request,
    head_ownership,
    open_owned_candidate,
    same_candidate,
)
from yaga.codex.constants import (
    AUTOMATIC_REACTION_ACTIONS,
    MAX_STATUSES_BEFORE_TERMINAL_WRITE,
    MAX_STATUSES_PER_CONTEXT,
)
from yaga.codex.evidence import CodexReviewRequiredError, check_codex_outcome_policy
from yaga.codex.models import Candidate
from yaga.codex.provenance import (
    ReviewBoundary,
    latest_lifecycle_history,
    newer_boundary,
    newer_ineligible_transition,
    source_boundary_action,
)
from yaga.codex.publication import (
    ensure_live_boundary_pending,
    ensure_live_uncertain_pending,
    ensure_pending_boundary,
    ensure_uncertain_pending,
    hold_pending_for_lifecycle_history,
    publish_pending_boundary,
    publish_success_with_pending_repair,
)
from yaga.codex.statuses import (
    authoritative_status,
    latest_status_context_page,
    status_context_usage,
    trusted_actions_status,
    workflow_run_url,
)
from yaga.errors import GateError
from yaga.github import RestApi, load_default_branch, load_pull_request
from yaga.models import PullRequest, commit_sha, positive_int, ref_name, repository_name


@dataclass(frozen=True)
class _LifecyclePreparation:
    """Result of making one exact candidate safe before ownership checks."""

    boundary: ReviewBoundary | None = None
    outcome: str | None = None


def _prepare_lifecycle_authority(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    candidate: Candidate,
    pull_request: PullRequest,
    reconciliation_run_id: int,
    boundary_cache: dict[int, ReviewBoundary | None],
) -> _LifecyclePreparation:
    """Make the newest lifecycle state physically pending before status history."""
    try:
        history = latest_lifecycle_history(
            api,
            repository=repository,
            head_sha=candidate.head_sha,
            not_before=pull_request.created_at,
            pull_request_number=candidate.pull_request_number,
        )
    except GateError as history_error:
        try:
            ensure_uncertain_pending(
                api,
                repository=repository,
                server_url=server_url,
                candidate=candidate,
                reconciliation_run_id=reconciliation_run_id,
            )
        except GateError as repair_error:
            raise GateError(
                f"{history_error}; fail-closed pending repair also failed: {repair_error}"
            ) from history_error
        raise
    source = history.source or (None if history.complete else history.newest_source)
    if (
        newer_ineligible_transition(
            history,
            history.source.boundary if history.source is not None else None,
        )
        is not None
    ):
        ensure_uncertain_pending(
            api,
            repository=repository,
            server_url=server_url,
            candidate=candidate,
            reconciliation_run_id=reconciliation_run_id,
        )
        return _LifecyclePreparation(
            outcome="pending: a newer close or draft transition has no review boundary"
        )
    if source is None:
        ensure_uncertain_pending(
            api,
            repository=repository,
            server_url=server_url,
            candidate=candidate,
            reconciliation_run_id=reconciliation_run_id,
        )
        return _LifecyclePreparation(outcome="pending: no trusted lifecycle boundary is available")
    boundary = source.boundary
    if not history.complete:
        confirmed = ensure_pending_boundary(
            api,
            repository=repository,
            server_url=server_url,
            candidate=candidate,
            boundary=boundary,
            boundary_cache=boundary_cache,
        )
        if confirmed is None:
            return _LifecyclePreparation(
                outcome="pending: pull request changed during lifecycle recovery"
            )
        return _LifecyclePreparation(
            outcome="pending: lifecycle history exceeded the bounded window"
        )
    if (
        boundary.pull_request_number,
        boundary.head_sha,
        boundary.base_sha,
        boundary.base_ref,
    ) != (
        candidate.pull_request_number,
        candidate.head_sha,
        candidate.base_sha,
        candidate.base_ref,
    ):
        confirmed = ensure_pending_boundary(
            api,
            repository=repository,
            server_url=server_url,
            candidate=candidate,
            boundary=boundary,
            boundary_cache=boundary_cache,
        )
        if confirmed is None:
            return _LifecyclePreparation(
                outcome="pending: pull request changed during lifecycle recovery"
            )
        return _LifecyclePreparation(
            outcome="pending: lifecycle boundary does not identify the exact candidate"
        )
    try:
        latest = latest_status_context_page(
            api,
            repository=repository,
            head_sha=candidate.head_sha,
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
    if latest is not None:
        latest_boundary = trusted_actions_status(
            api,
            latest,
            server_url=server_url,
            repository=repository,
            head_sha=candidate.head_sha,
            boundary_cache=boundary_cache,
        )
        if latest_boundary == boundary and latest.state == "success":
            return _LifecyclePreparation(boundary=boundary)
        if latest_boundary == boundary and latest.state == "pending":
            return _LifecyclePreparation(boundary=boundary)
    confirmed = ensure_pending_boundary(
        api,
        repository=repository,
        server_url=server_url,
        candidate=candidate,
        boundary=boundary,
        boundary_cache=boundary_cache,
    )
    if confirmed is None:
        return _LifecyclePreparation(
            outcome="pending: pull request changed during lifecycle recovery"
        )
    return _LifecyclePreparation(boundary=boundary)


def reconcile_exact_candidate(
    api: RestApi,
    *,
    repository: str,
    server_url: str,
    expected_pull_request: int,
    expected_head: str,
    expected_base_sha: str,
    expected_base_ref: str,
    expected_head_repository: str,
    expected_head_ref: str,
    reconciliation_run_id: int,
) -> str:
    """Reconcile one already-pending exact candidate without requesting a review."""
    repository = repository_name(repository)
    expected_pull_request = positive_int(expected_pull_request, "expected pull request number")
    expected_head = commit_sha(expected_head, "expected pull request head")
    expected_base_sha = commit_sha(expected_base_sha, "expected pull request base")
    expected_base_ref = ref_name(expected_base_ref)
    expected_head_repository = repository_name(expected_head_repository)
    expected_head_ref = ref_name(expected_head_ref)
    reconciliation_run_id = positive_int(reconciliation_run_id, "reconciliation workflow run ID")
    workflow_run_url(server_url, repository, 1)
    candidate = Candidate(
        pull_request_number=expected_pull_request,
        head_sha=expected_head,
        base_sha=expected_base_sha,
        base_ref=expected_base_ref,
        head_repository=expected_head_repository,
        head_ref=expected_head_ref,
    )
    live_default_branch = load_default_branch(api, repository)
    pull_request = load_pull_request(api, repository, expected_pull_request)
    if pull_request.state != "open":
        return "skipped: pull request is closed"
    if pull_request.base_ref != live_default_branch:
        return "skipped: pull request does not target the repository default branch"
    if pull_request.draft or candidate_from_pull_request(pull_request) != candidate:
        live_candidate = candidate_from_pull_request(pull_request)
        confirmed = ensure_live_uncertain_pending(
            api,
            repository=repository,
            server_url=server_url,
            candidate=live_candidate,
            reconciliation_run_id=reconciliation_run_id,
        )
        return (
            "pending: pull request changed before lifecycle preparation"
            if confirmed is not None
            else "pending: pull request changed during initial repair"
        )
    boundary_cache: dict[int, ReviewBoundary | None] = {}
    recovery = _prepare_lifecycle_authority(
        api,
        repository=repository,
        server_url=server_url,
        candidate=candidate,
        pull_request=pull_request,
        reconciliation_run_id=reconciliation_run_id,
        boundary_cache=boundary_cache,
    )
    if recovery.outcome is not None:
        return recovery.outcome
    if recovery.boundary is None:
        raise GateError("lifecycle preparation omitted the exact boundary")
    try:
        live_pull_request = load_pull_request(api, repository, expected_pull_request)
        live_default_branch = load_default_branch(api, repository)
    except GateError as state_error:
        try:
            ensure_pending_boundary(
                api,
                repository=repository,
                server_url=server_url,
                candidate=candidate,
                boundary=recovery.boundary,
                boundary_cache=boundary_cache,
            )
        except GateError as repair_error:
            raise GateError(
                f"{state_error}; fail-closed pending repair also failed: {repair_error}"
            ) from state_error
        raise
    if live_pull_request.state != "open":
        return "skipped: pull request closed before ownership validation"
    if live_pull_request.base_ref != live_default_branch:
        return "skipped: pull request no longer targets the repository default branch"

    live_candidate = candidate_from_pull_request(live_pull_request)
    if live_pull_request.head_sha != expected_head:
        confirmed = ensure_live_uncertain_pending(
            api,
            repository=repository,
            server_url=server_url,
            candidate=live_candidate,
            reconciliation_run_id=reconciliation_run_id,
        )
        return (
            "pending: pull request head changed before ownership validation"
            if confirmed is not None
            else "pending: pull request changed during new-head repair"
        )
    if live_pull_request.draft or not same_candidate(pull_request, live_pull_request):
        confirmed = ensure_live_boundary_pending(
            api,
            repository=repository,
            server_url=server_url,
            candidate=live_candidate,
            boundary=recovery.boundary,
        )
        return (
            "pending: pull request state changed before ownership validation"
            if confirmed is not None
            else "pending: pull request changed during state repair"
        )

    try:
        ownership = head_ownership(api, repository=repository, head_sha=expected_head)
    except GateError as ownership_error:
        try:
            ensure_live_boundary_pending(
                api,
                repository=repository,
                server_url=server_url,
                candidate=live_candidate,
                boundary=recovery.boundary,
            )
        except GateError as repair_error:
            raise GateError(
                f"{ownership_error}; fail-closed pending repair also failed: {repair_error}"
            ) from ownership_error
        raise
    owned_pull_request = ownership.pull_request
    if (
        owned_pull_request is None
        or owned_pull_request.draft
        or not same_candidate(live_pull_request, owned_pull_request)
    ):
        repair_candidate = candidate_from_pull_request(
            owned_pull_request if owned_pull_request is not None else live_pull_request
        )
        confirmed = ensure_live_boundary_pending(
            api,
            repository=repository,
            server_url=server_url,
            candidate=repair_candidate,
            boundary=recovery.boundary,
        )
        return (
            "pending: Codex review head is not uniquely owned by the source pull request"
            if confirmed is not None
            else "pending: pull request changed during ownership repair"
        )
    pull_request = owned_pull_request
    initial_usage = status_context_usage(api, repository=repository, head_sha=expected_head)
    authoritative = authoritative_status(
        api,
        initial_usage,
        server_url=server_url,
        repository=repository,
        pull_request_number=expected_pull_request,
        head_sha=expected_head,
        base_sha=expected_base_sha,
        base_ref=expected_base_ref,
        boundary_cache=boundary_cache,
    )
    if authoritative is None:
        return "skipped: prepared lifecycle boundary was superseded"
    status, boundary = authoritative
    latest = initial_usage.latest
    if latest is None:
        raise GateError("Codex review status history is empty")
    race_floor: ReviewBoundary | None = None
    head_authoritative = authoritative_status(
        api,
        initial_usage,
        server_url=server_url,
        repository=repository,
        pull_request_number=None,
        head_sha=expected_head,
        base_sha=None,
        base_ref=None,
        boundary_cache=boundary_cache,
    )
    if head_authoritative is not None:
        head_boundary = head_authoritative[1]
        if (head_boundary.occurred_at, head_boundary.workflow_run_id) > (
            boundary.occurred_at,
            boundary.workflow_run_id,
        ):
            if not boundary_is_displaced(
                api,
                repository=repository,
                head_sha=expected_head,
                current_pull_request=pull_request,
                boundary=head_boundary,
            ):
                return "skipped: a newer semantic Codex boundary owns the head"
            race_floor = newer_boundary(race_floor, head_boundary)

    if latest.status_id != status.status_id:
        latest_boundary = trusted_actions_status(
            api,
            latest,
            server_url=server_url,
            repository=repository,
            head_sha=expected_head,
            boundary_cache=boundary_cache,
        )
        if latest_boundary is None:
            return "skipped: a newer physical Codex status owns the head"
        if (latest_boundary.occurred_at, latest_boundary.workflow_run_id) >= (
            boundary.occurred_at,
            boundary.workflow_run_id,
        ):
            if not boundary_is_displaced(
                api,
                repository=repository,
                head_sha=expected_head,
                current_pull_request=pull_request,
                boundary=latest_boundary,
            ):
                return "skipped: a newer physical Codex status owns the head"
            race_floor = newer_boundary(race_floor, latest_boundary)
        repair_limit = (
            MAX_STATUSES_PER_CONTEXT
            if status.state == "pending"
            else MAX_STATUSES_BEFORE_TERMINAL_WRITE
        )
        if initial_usage.count >= repair_limit:
            raise GateError("Codex review status capacity is exhausted")
        confirmed = open_owned_candidate(
            api,
            repository=repository,
            pull_request_number=expected_pull_request,
            expected_head=expected_head,
            expected_base_sha=expected_base_sha,
            expected_base_ref=expected_base_ref,
            expected_head_repository=expected_head_repository,
            expected_head_ref=expected_head_ref,
        )
        if not same_candidate(pull_request, confirmed):
            return "pending: pull request changed before status-order repair"
        repair_usage = status_context_usage(api, repository=repository, head_sha=expected_head)
        repair_authoritative = authoritative_status(
            api,
            repair_usage,
            server_url=server_url,
            repository=repository,
            pull_request_number=expected_pull_request,
            head_sha=expected_head,
            base_sha=expected_base_sha,
            base_ref=expected_base_ref,
            boundary_cache=boundary_cache,
        )
        if (
            repair_authoritative is None
            or repair_authoritative[0].status_id != status.status_id
            or repair_authoritative[1] != boundary
            or repair_usage.latest is None
            or repair_usage.latest.status_id != latest.status_id
        ):
            return "skipped: Codex status changed before status-order repair"
        if repair_usage.count >= repair_limit:
            raise GateError("Codex review status capacity is exhausted")
        if status.state == "success":
            lifecycle_hold = hold_pending_for_lifecycle_history(
                api,
                repository=repository,
                server_url=server_url,
                pull_request=pull_request,
                boundary=boundary,
                boundary_cache=boundary_cache,
            )
            if lifecycle_hold is not None:
                return lifecycle_hold
            raced_pending = publish_success_with_pending_repair(
                api,
                repository=repository,
                server_url=server_url,
                pull_request=pull_request,
                boundary=boundary,
                target_url=status.target_url,
                boundary_cache=boundary_cache,
                race_floor=race_floor,
            )
            if raced_pending:
                return "pending: a newer boundary raced durable-success restoration"
            return "success: restored the authoritative durable Codex result"
        publish_pending_boundary(
            api,
            repository=repository,
            head_sha=expected_head,
            boundary=boundary,
            target_url=status.target_url,
        )
        if repair_usage.count == MAX_STATUSES_PER_CONTEXT - 1:
            return f"pending for {pull_request.head_sha}: Codex review status capacity is exhausted"
        initial_usage = status_context_usage(api, repository=repository, head_sha=expected_head)
        authoritative = authoritative_status(
            api,
            initial_usage,
            server_url=server_url,
            repository=repository,
            pull_request_number=expected_pull_request,
            head_sha=expected_head,
            base_sha=expected_base_sha,
            base_ref=expected_base_ref,
            boundary_cache=boundary_cache,
        )
        if (
            authoritative is None
            or authoritative[0].state != "pending"
            or initial_usage.latest is None
            or initial_usage.latest.status_id != authoritative[0].status_id
        ):
            return "skipped: repaired pending status was superseded"
        status, boundary = authoritative

    lifecycle_hold = hold_pending_for_lifecycle_history(
        api,
        repository=repository,
        server_url=server_url,
        pull_request=pull_request,
        boundary=boundary,
        boundary_cache=boundary_cache,
    )
    if lifecycle_hold is not None:
        return lifecycle_hold

    if status.state == "success":
        return "skipped: exact candidate already has durable Codex review success"

    boundary_action = source_boundary_action(api, repository=repository, expected=boundary)
    if boundary_action == "edited":
        # Connector outcomes identify the reviewed head, not the base SHA. A
        # review already in flight when the base changes can complete after this
        # boundary while still describing the old candidate. Keep the changed
        # base fail-closed until synchronize records a new head boundary.
        return (
            f"pending for {pull_request.head_sha}: "
            "base change requires a new head before Codex evidence can pass"
        )
    # Reopen and ready-for-review do not change the code or base captured by the
    # boundary. A post-boundary exact-head comment/review is code-equivalent even
    # when the connector does not expose when its review work began. Reactions
    # remain initial-open-only because they carry no commit marker.
    allow_clean_reaction = boundary_action in AUTOMATIC_REACTION_ACTIONS

    try:
        result = check_codex_outcome_policy(
            api,
            repository=repository,
            pull_request=pull_request,
            not_before=boundary.occurred_at,
            allow_clean_reaction=allow_clean_reaction,
        )
    except CodexReviewRequiredError as error:
        return f"pending for {pull_request.head_sha}: {error}"

    final_candidate = open_owned_candidate(
        api,
        repository=repository,
        pull_request_number=expected_pull_request,
        expected_head=expected_head,
        expected_base_sha=expected_base_sha,
        expected_base_ref=expected_base_ref,
        expected_head_repository=expected_head_repository,
        expected_head_ref=expected_head_ref,
    )
    if not same_candidate(pull_request, final_candidate):
        return "pending: pull request closed, became draft, or changed during evaluation"

    try:
        result = check_codex_outcome_policy(
            api,
            repository=repository,
            pull_request=pull_request,
            not_before=boundary.occurred_at,
            allow_clean_reaction=allow_clean_reaction,
        )
    except CodexReviewRequiredError as error:
        return f"pending for {pull_request.head_sha}: {error}"
    post_policy_candidate = open_owned_candidate(
        api,
        repository=repository,
        pull_request_number=expected_pull_request,
        expected_head=expected_head,
        expected_base_sha=expected_base_sha,
        expected_base_ref=expected_base_ref,
        expected_head_repository=expected_head_repository,
        expected_head_ref=expected_head_ref,
    )
    if not same_candidate(pull_request, post_policy_candidate):
        return "pending: pull request changed after Codex outcome confirmation"

    final_usage = status_context_usage(api, repository=repository, head_sha=expected_head)
    final_authoritative = authoritative_status(
        api,
        final_usage,
        server_url=server_url,
        repository=repository,
        pull_request_number=expected_pull_request,
        head_sha=expected_head,
        base_sha=expected_base_sha,
        base_ref=expected_base_ref,
        boundary_cache=boundary_cache,
    )
    if (
        final_authoritative is None
        or final_authoritative[0].status_id != status.status_id
        or final_authoritative[1] != boundary
        or final_usage.latest is None
        or final_usage.latest.status_id != status.status_id
    ):
        return "skipped: a newer Codex review publisher owns the status"
    if final_usage.count >= MAX_STATUSES_BEFORE_TERMINAL_WRITE:
        raise GateError("Codex review status capacity is exhausted")

    lifecycle_hold = hold_pending_for_lifecycle_history(
        api,
        repository=repository,
        server_url=server_url,
        pull_request=pull_request,
        boundary=boundary,
        boundary_cache=boundary_cache,
    )
    if lifecycle_hold is not None:
        return lifecycle_hold

    raced_pending = publish_success_with_pending_repair(
        api,
        repository=repository,
        server_url=server_url,
        pull_request=pull_request,
        boundary=boundary,
        target_url=status.target_url,
        boundary_cache=boundary_cache,
        race_floor=race_floor,
    )
    if raced_pending:
        return "pending: a newer boundary raced Codex success publication"
    return f"success for {pull_request.head_sha}: {result}"
