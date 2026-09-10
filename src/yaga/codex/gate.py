"""Trusted lifecycle, post-CI review, and final gate operations."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from yaga.codex.boundary import (
    ReviewBoundary,
    boundary_description,
    parse_boundary_description,
    same_boundary,
)
from yaga.codex.candidate import LiveCandidate, load_live_candidate
from yaga.codex.constants import (
    CI_FAILURE_DESCRIPTION,
    CI_STATUS_CONTEXT,
    CODEX_STATUS_CONTEXT,
    ERROR_WRITE_REQUEST_RESERVE,
    GITHUB_ACTIONS_LOGIN,
    GITHUB_ACTIONS_USER_ID,
    LIFECYCLE_STATUS_SLOT_RESERVE,
    POLL_INTERVAL_SECONDS,
    POLL_ITERATION_REQUEST_RESERVE,
    SUCCESS_CLEANUP_MARGIN_SECONDS,
    SUCCESS_WRITE_REQUEST_RESERVE,
    TIMEOUT_DESCRIPTION,
)
from yaga.codex.events import parse_codex_event, parse_event_boundary
from yaga.codex.evidence import (
    CodexOutcome,
    CodexReviewRequiredError,
    has_codex_pending_reaction,
    select_codex_outcome,
    validate_codex_outcome,
)
from yaga.codex.publication import (
    StatusLease,
    ensure_status_capacity,
    latest_trusted_status,
    lease_is_current,
    load_exact_live_pull_request,
    publish_pending,
    publish_terminal,
    restore_pending_after_race,
    status_history,
    terminal_has_pending_lineage,
    terminal_is_current,
    uniquely_owned,
)
from yaga.codex.requests import (
    RequestComment,
    RequestKey,
    ensure_authorization,
    find_authorization,
    validate_authorization,
)
from yaga.codex.runs import SourceRun, require_current_source
from yaga.errors import GateError
from yaga.github import RestApi, require_success_tail
from yaga.models import positive_int, workflow_path
from yaga.status import CommitStatus


@dataclass(frozen=True)
class GateResult:
    """One user-visible operation result, exit status, and closed route."""

    message: str
    exit_code: int
    route: str = "skip"


class _RequestAlreadyCoveredError(Exception):
    """Stop the one request POST when its exact marker appears at the last read."""


class _UnsolicitedCodexActivityError(Exception):
    """Stop the request POST when connector activity lacks a YAGA request."""


def invalidate(
    api: RestApi,
    *,
    repository: str,
    event_name: str,
    event: dict[str, object],
    run_id: int,
    run_attempt: int,
    target_url: str,
) -> GateResult:
    """Invalidate inherited successes behind the required native lifecycle check."""
    local = parse_event_boundary(repository=repository, event_name=event_name, event=event)
    if local.disposition in {"closed", "noop"}:
        return GateResult("skipped: event is not a review boundary", 0)
    if (
        local.head_sha is None
        or local.base_sha is None
        or local.occurred_at is None
        or local.base_ref is None
    ):
        raise GateError("active lifecycle boundary is incomplete")
    boundary = ReviewBoundary(
        run_id=positive_int(run_id, "lifecycle run ID"),
        run_attempt=positive_int(run_attempt, "lifecycle run attempt"),
        action=local.action,
        pull_request_number=local.pull_request_number,
        head_sha=local.head_sha,
        base_sha=local.base_sha,
        occurred_at=local.occurred_at,
    )
    parsed = parse_codex_event(repository=repository, event_name=event_name, event=event)
    if parsed.pull_request is None:
        raise GateError("active lifecycle event has no pull request")
    live = load_exact_live_pull_request(
        api,
        repository=repository,
        expected=parsed.pull_request,
        default_branch=parsed.default_branch,
        require_ready=parsed.disposition == "review",
    )
    if live is None or live.draft != parsed.pull_request.draft:
        return GateResult("skipped: lifecycle candidate changed before invalidation", 0)
    if not uniquely_owned(
        api,
        repository=repository,
        head_sha=boundary.head_sha,
        pull_request_number=boundary.pull_request_number,
    ):
        raise GateError("active lifecycle head is not uniquely owned by this pull request")
    ensure_status_capacity(
        api,
        repository=repository,
        head_sha=boundary.head_sha,
        contexts=(CODEX_STATUS_CONTEXT, CI_STATUS_CONTEXT),
        reserve_per_context=LIFECYCLE_STATUS_SLOT_RESERVE,
    )
    description = boundary_description(boundary, "CI pending")
    publish_pending(
        api,
        repository=repository,
        head_sha=boundary.head_sha,
        target_url=target_url,
        description=description,
        context=CODEX_STATUS_CONTEXT,
    )
    if parsed.default_branch != local.default_branch or parsed.disposition != local.disposition:
        raise GateError("lifecycle event changed during staged validation")
    publish_pending(
        api,
        repository=repository,
        head_sha=boundary.head_sha,
        target_url=target_url,
        description=description,
        context=CI_STATUS_CONTEXT,
    )
    if local.disposition == "draft":
        return GateResult("pending: pull request is a draft", 0)
    if local.base_ref != local.default_branch:
        return GateResult("pending: pull request no longer targets the default branch", 0)
    return GateResult("pending: waiting for exact-head CI prerequisites", 0)


def _same_candidate(left: LiveCandidate, right: LiveCandidate) -> bool:
    return (
        left.pull_request == right.pull_request
        and left.author_id == right.author_id
        and left.author_login == right.author_login
        and left.default_branch == right.default_branch
        and same_boundary(left.boundary, right.boundary)
        and left.lifecycle_updated_at == right.lifecycle_updated_at
    )


def _elected_wake_workflow(
    *,
    source: SourceRun,
    candidate: LiveCandidate,
    lifecycle_workflow: str,
) -> str:
    """Elect exactly one of the two completion wakes before any gate write.

    The workflow that completed later owns reconciliation. Exact timestamp ties
    go to CI: that wake necessarily has a completed source and can already be
    waiting for the lifecycle invalidator, while the inverse is not guaranteed.
    """
    if candidate.lifecycle_updated_at > source.updated_at:
        return lifecycle_workflow
    return source.workflow_path


def _load_current(
    api: RestApi,
    *,
    repository: str,
    source: SourceRun,
    lifecycle_workflow: str,
    server_url: str,
) -> LiveCandidate | None:
    require_current_source(api, repository=repository, source=source)
    return load_live_candidate(
        api,
        repository=repository,
        source=source,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
    )


def _revalidate(
    api: RestApi,
    *,
    repository: str,
    source: SourceRun,
    candidate: LiveCandidate,
    lifecycle_workflow: str,
    server_url: str,
    lease: StatusLease | None = None,
) -> bool:
    current = _load_current(
        api,
        repository=repository,
        source=source,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
    )
    if current is None or not _same_candidate(candidate, current):
        return False
    if lease is not None and not lease_is_current(api, repository=repository, lease=lease):
        return False
    if not uniquely_owned(
        api,
        repository=repository,
        head_sha=candidate.pull_request.head_sha,
        pull_request_number=candidate.pull_request.number,
    ):
        return False
    return (
        load_exact_live_pull_request(
            api,
            repository=repository,
            expected=candidate.pull_request,
            default_branch=candidate.default_branch,
            require_ready=True,
        )
        is not None
    )


def _request_key(repository: str, source: SourceRun, candidate: LiveCandidate) -> RequestKey:
    return RequestKey(
        repository=repository,
        pull_request_number=candidate.pull_request.number,
        head_sha=candidate.pull_request.head_sha,
        base_sha=candidate.pull_request.base_sha,
        boundary_run_id=candidate.boundary.run_id,
        source_run_id=source.run_id,
        source_run_attempt=source.run_attempt,
    )


def _publish_codex_pending(
    api: RestApi,
    *,
    repository: str,
    candidate: LiveCandidate,
    target_url: str,
) -> StatusLease:
    return publish_pending(
        api,
        repository=repository,
        head_sha=candidate.pull_request.head_sha,
        target_url=target_url,
        description=boundary_description(candidate.boundary, "Codex pending"),
        context=CODEX_STATUS_CONTEXT,
    )


def _publish_ci_pending(
    api: RestApi,
    *,
    repository: str,
    candidate: LiveCandidate,
    target_url: str,
) -> StatusLease:
    return publish_pending(
        api,
        repository=repository,
        head_sha=candidate.pull_request.head_sha,
        target_url=target_url,
        description=boundary_description(candidate.boundary, "CI pending"),
        context=CI_STATUS_CONTEXT,
    )


def _current_outcome(
    api: RestApi,
    *,
    repository: str,
    candidate: LiveCandidate,
    reaction_request: RequestComment | None = None,
) -> CodexOutcome | None:
    evidence_not_before = _evidence_not_before(candidate, reaction_request)
    if evidence_not_before is None:
        return None
    try:
        return select_codex_outcome(
            api,
            repository=repository,
            pull_request=candidate.pull_request,
            not_before=evidence_not_before,
            allow_clean_reaction=True,
            clean_reaction_not_before=evidence_not_before + timedelta(seconds=1),
        )
    except CodexReviewRequiredError:
        return None


def _outcome_capability_is_current(
    api: RestApi,
    *,
    repository: str,
    candidate: LiveCandidate,
    outcome: CodexOutcome | None,
    reaction_request: RequestComment | None = None,
) -> bool:
    if outcome is None:
        return False
    evidence_not_before = _evidence_not_before(candidate, reaction_request)
    if evidence_not_before is None:
        return False
    return validate_codex_outcome(
        api,
        repository=repository,
        pull_request=candidate.pull_request,
        not_before=evidence_not_before,
        allow_clean_reaction=True,
        outcome=outcome,
        clean_reaction_not_before=evidence_not_before + timedelta(seconds=1),
    )


def _evidence_not_before(
    candidate: LiveCandidate,
    request: RequestComment | None,
) -> datetime | None:
    if request is None or request.kind != "request" or request.created_at is None:
        return None
    return max(candidate.boundary.occurred_at, request.created_at)


def _request_capability_is_current(
    api: RestApi,
    *,
    key: RequestKey | None,
    request: RequestComment | None,
) -> bool:
    if key is None or request is None or request.kind != "request":
        return False
    try:
        return validate_authorization(api, key=key, comment=request)
    except GateError:
        return False


def _unsolicited_activity_is_visible(
    api: RestApi,
    *,
    repository: str,
    candidate: LiveCandidate,
) -> bool:
    """Detect current-bound connector activity without trusting it as evidence."""
    boundary = candidate.boundary.occurred_at
    if has_codex_pending_reaction(
        api,
        repository=repository,
        pull_request=candidate.pull_request,
        not_before=boundary,
    ):
        return True
    try:
        select_codex_outcome(
            api,
            repository=repository,
            pull_request=candidate.pull_request,
            # Outcome selectors use a strict comparison. Include evidence in
            # the boundary second because its ordering is ambiguous and must
            # suppress a duplicate request.
            not_before=boundary - timedelta(seconds=1),
            allow_clean_reaction=True,
            clean_reaction_not_before=boundary,
        )
    except CodexReviewRequiredError:
        return False
    return True


def _prior_requests_are_settled(
    api: RestApi,
    *,
    repository: str,
    requests: tuple[RequestComment, ...],
) -> bool:
    """Require trusted successful lineage for every older YAGA request."""
    for request in requests:
        created_at = request.created_at
        if created_at is None:
            raise GateError("prior Codex request timestamp is missing")
        history = status_history(
            api,
            repository=repository,
            head_sha=request.key.head_sha,
            context=CODEX_STATUS_CONTEXT,
        )
        settled = False
        for index, terminal in enumerate(history[:-1]):
            predecessor = history[index + 1]
            parsed = parse_boundary_description(terminal, head_sha=request.key.head_sha)
            if parsed is None or parsed[1] != "Codex passed":
                continue
            boundary = parsed[0]
            actions_owned = (
                terminal.context == CODEX_STATUS_CONTEXT
                and predecessor.context == CODEX_STATUS_CONTEXT
                and terminal.creator_id == GITHUB_ACTIONS_USER_ID
                and terminal.creator_login == GITHUB_ACTIONS_LOGIN
                and predecessor.creator_id == GITHUB_ACTIONS_USER_ID
                and predecessor.creator_login == GITHUB_ACTIONS_LOGIN
            )
            if (
                terminal.state == "success"
                and terminal.created_at > created_at
                and terminal.target_url is not None
                and boundary.run_id == request.key.boundary_run_id
                and boundary.pull_request_number == request.key.pull_request_number
                and boundary.head_sha == request.key.head_sha
                and boundary.base_sha == request.key.base_sha
                and actions_owned
                and predecessor.state == "pending"
                and predecessor.target_url == terminal.target_url
                and predecessor.description == boundary_description(boundary, "Codex pending")
            ):
                settled = True
                break
        if not settled:
            return False
    return True


def _request_history_is_settled(
    api: RestApi,
    *,
    repository: str,
    key: RequestKey | None,
) -> bool:
    """Re-read the current key and reject newly visible unsettled requests."""
    if key is None:
        return False
    return _prior_requests_are_settled(
        api,
        repository=repository,
        requests=find_authorization(api, key=key).prior_requests,
    )


def _authorization_capability_is_current(
    api: RestApi,
    *,
    key: RequestKey | None,
    authorization: RequestComment | None,
) -> bool:
    if authorization is None:
        return True
    return key is not None and validate_authorization(api, key=key, comment=authorization)


def _request_follows_authorization(
    request: RequestComment | None,
    authorization: RequestComment | None,
) -> bool:
    """Require an external request to be strictly later than its approval."""
    if authorization is None:
        return True
    return bool(
        authorization.kind == "approval"
        and authorization.created_at is not None
        and request is not None
        and request.kind == "request"
        and request.created_at is not None
        and request.created_at > authorization.created_at
    )


def _codex_status_capability_is_current(
    api: RestApi,
    *,
    repository: str,
    candidate: LiveCandidate,
    expected: CommitStatus | None,
) -> bool:
    if expected is None:
        return False
    if expected.state != "success" or not terminal_has_pending_lineage(
        api,
        repository=repository,
        head_sha=candidate.pull_request.head_sha,
        terminal_status=expected,
        pending_description=boundary_description(candidate.boundary, "Codex pending"),
    ):
        return False
    parsed = parse_boundary_description(expected, head_sha=candidate.pull_request.head_sha)
    return bool(
        parsed is not None
        and parsed[1] == "Codex passed"
        and same_boundary(parsed[0], candidate.boundary)
    )


def _settle_codex(
    api: RestApi,
    *,
    repository: str,
    source: SourceRun,
    candidate: LiveCandidate,
    lifecycle_workflow: str,
    server_url: str,
    lease: StatusLease,
    success: bool,
    outcome: CodexOutcome | None = None,
    authorization: RequestComment | None = None,
    reaction_request: RequestComment | None = None,
    request_key: RequestKey | None = None,
) -> bool:
    if success:
        if outcome is None or request_key is None:
            raise GateError("Codex success capabilities are incomplete")
        require_success_tail(
            api,
            SUCCESS_WRITE_REQUEST_RESERVE,
            cleanup_margin_seconds=SUCCESS_CLEANUP_MARGIN_SECONDS,
        )
    if not _revalidate(
        api,
        repository=repository,
        source=source,
        candidate=candidate,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
        lease=lease,
    ):
        return False
    if success and (
        not _outcome_capability_is_current(
            api,
            repository=repository,
            candidate=candidate,
            outcome=outcome,
            reaction_request=reaction_request,
        )
        or not _request_capability_is_current(
            api,
            key=request_key,
            request=reaction_request,
        )
        or not _authorization_capability_is_current(
            api,
            key=request_key,
            authorization=authorization,
        )
        or not _request_follows_authorization(reaction_request, authorization)
        or not _request_history_is_settled(
            api,
            repository=repository,
            key=request_key,
        )
    ):
        return False
    terminal = publish_terminal(
        api,
        repository=repository,
        lease=lease,
        state="success" if success else "error",
        description=boundary_description(
            candidate.boundary,
            "Codex passed" if success else "Codex failed",
        ),
    )
    if terminal is None:
        return False
    try:
        if not terminal_is_current(
            api,
            repository=repository,
            lease=lease,
            terminal_status=terminal,
        ):
            restore_pending_after_race(
                api,
                repository=repository,
                lease=lease,
                terminal_status=terminal,
            )
            return False
        if not _revalidate(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
        ):
            restore_pending_after_race(
                api,
                repository=repository,
                lease=lease,
                terminal_status=terminal,
            )
            return False
        if success and (
            not _outcome_capability_is_current(
                api,
                repository=repository,
                candidate=candidate,
                outcome=outcome,
                reaction_request=reaction_request,
            )
            or not _request_capability_is_current(
                api,
                key=request_key,
                request=reaction_request,
            )
            or not _authorization_capability_is_current(
                api,
                key=request_key,
                authorization=authorization,
            )
            or not _request_follows_authorization(reaction_request, authorization)
            or not _request_history_is_settled(
                api,
                repository=repository,
                key=request_key,
            )
        ):
            restore_pending_after_race(
                api,
                repository=repository,
                lease=lease,
                terminal_status=terminal,
            )
            return False
        if not terminal_is_current(
            api,
            repository=repository,
            lease=lease,
            terminal_status=terminal,
        ):
            restore_pending_after_race(
                api,
                repository=repository,
                lease=lease,
                terminal_status=terminal,
            )
            return False
    except GateError:
        restore_pending_after_race(
            api,
            repository=repository,
            lease=lease,
            terminal_status=terminal,
        )
        raise
    return True


def _settle_ci(
    api: RestApi,
    *,
    repository: str,
    source: SourceRun,
    candidate: LiveCandidate,
    lifecycle_workflow: str,
    server_url: str,
    target_url: str,
    success: bool,
    codex_status: CommitStatus | None = None,
    outcome: CodexOutcome | None = None,
    authorization: RequestComment | None = None,
    reaction_request: RequestComment | None = None,
    request_key: RequestKey | None = None,
) -> bool:
    if success:
        if codex_status is None or outcome is None or request_key is None:
            raise GateError("CI success capabilities are incomplete")
        require_success_tail(
            api,
            SUCCESS_WRITE_REQUEST_RESERVE,
            cleanup_margin_seconds=SUCCESS_CLEANUP_MARGIN_SECONDS,
        )
    if not _revalidate(
        api,
        repository=repository,
        source=source,
        candidate=candidate,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
    ):
        return False
    lease = _publish_ci_pending(
        api,
        repository=repository,
        candidate=candidate,
        target_url=target_url,
    )
    if not _revalidate(
        api,
        repository=repository,
        source=source,
        candidate=candidate,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
        lease=lease,
    ):
        return False
    if success and (
        not _codex_status_capability_is_current(
            api,
            repository=repository,
            candidate=candidate,
            expected=codex_status,
        )
        or not _outcome_capability_is_current(
            api,
            repository=repository,
            candidate=candidate,
            outcome=outcome,
            reaction_request=reaction_request,
        )
        or not _request_capability_is_current(
            api,
            key=request_key,
            request=reaction_request,
        )
        or not _authorization_capability_is_current(
            api,
            key=request_key,
            authorization=authorization,
        )
        or not _request_follows_authorization(reaction_request, authorization)
        or not _request_history_is_settled(
            api,
            repository=repository,
            key=request_key,
        )
    ):
        return False
    terminal = publish_terminal(
        api,
        repository=repository,
        lease=lease,
        state="success" if success else "error",
        description=boundary_description(
            candidate.boundary,
            "CI passed" if success else "CI failed",
        ),
    )
    if terminal is None:
        return False
    try:
        if not terminal_is_current(
            api,
            repository=repository,
            lease=lease,
            terminal_status=terminal,
        ):
            restore_pending_after_race(
                api,
                repository=repository,
                lease=lease,
                terminal_status=terminal,
            )
            return False
        if not _revalidate(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
        ):
            restore_pending_after_race(
                api,
                repository=repository,
                lease=lease,
                terminal_status=terminal,
            )
            return False
        if success and (
            not _codex_status_capability_is_current(
                api,
                repository=repository,
                candidate=candidate,
                expected=codex_status,
            )
            or not _outcome_capability_is_current(
                api,
                repository=repository,
                candidate=candidate,
                outcome=outcome,
                reaction_request=reaction_request,
            )
            or not _request_capability_is_current(
                api,
                key=request_key,
                request=reaction_request,
            )
            or not _authorization_capability_is_current(
                api,
                key=request_key,
                authorization=authorization,
            )
            or not _request_follows_authorization(reaction_request, authorization)
            or not _request_history_is_settled(
                api,
                repository=repository,
                key=request_key,
            )
        ):
            restore_pending_after_race(
                api,
                repository=repository,
                lease=lease,
                terminal_status=terminal,
            )
            return False
        if not terminal_is_current(
            api,
            repository=repository,
            lease=lease,
            terminal_status=terminal,
        ):
            restore_pending_after_race(
                api,
                repository=repository,
                lease=lease,
                terminal_status=terminal,
            )
            return False
    except GateError:
        restore_pending_after_race(
            api,
            repository=repository,
            lease=lease,
            terminal_status=terminal,
        )
        raise
    return True


def prepare(
    api: RestApi,
    *,
    repository: str,
    source: SourceRun,
    lifecycle_workflow: str,
    wake_workflow: str,
    server_url: str,
    target_url: str,
    direct_author_id: int,
    lifecycle_deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> GateResult:
    """Classify one successful CI run without consuming review quota."""
    while True:
        try:
            candidate = _load_current(
                api,
                repository=repository,
                source=source,
                lifecycle_workflow=lifecycle_workflow,
                server_url=server_url,
            )
            break
        except GateError:
            if lifecycle_deadline is None:
                raise
            remaining = lifecycle_deadline - clock()
            if remaining <= 0:
                raise
            sleep(min(float(POLL_INTERVAL_SECONDS), remaining))
    if candidate is None:
        return GateResult("skipped: source candidate is no longer active", 0)
    if not _revalidate(
        api,
        repository=repository,
        source=source,
        candidate=candidate,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
    ):
        return GateResult("skipped: source candidate changed before publication", 0)
    wake_workflow = workflow_path(wake_workflow, "publisher wake workflow")
    if wake_workflow not in {source.workflow_path, lifecycle_workflow}:
        raise GateError("publisher wake workflow is not allowed")
    if wake_workflow != _elected_wake_workflow(
        source=source,
        candidate=candidate,
        lifecycle_workflow=lifecycle_workflow,
    ):
        return GateResult("skipped: another completion wake owns this boundary", 0)
    if source.conclusion != "success":
        lease = _publish_codex_pending(
            api,
            repository=repository,
            candidate=candidate,
            target_url=target_url,
        )
        _settle_codex(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            lease=lease,
            success=False,
        )
        _settle_ci(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            target_url=target_url,
            success=False,
        )
        return GateResult(CI_FAILURE_DESCRIPTION, 1)

    direct_author_id = positive_int(direct_author_id, "direct review author ID")
    lease = _publish_codex_pending(
        api,
        repository=repository,
        candidate=candidate,
        target_url=target_url,
    )
    _publish_ci_pending(
        api,
        repository=repository,
        candidate=candidate,
        target_url=target_url,
    )
    key = _request_key(repository, source, candidate)
    authorizations = find_authorization(api, key=key)
    owner_authored = candidate.author_id == direct_author_id
    authorization = None if owner_authored else authorizations.approval
    reaction_request = authorizations.request
    reaction_history_is_settled = _prior_requests_are_settled(
        api,
        repository=repository,
        requests=authorizations.prior_requests,
    )

    def fail_closed(message: str) -> GateResult:
        _settle_codex(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            lease=lease,
            success=False,
        )
        _settle_ci(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            target_url=target_url,
            success=False,
        )
        return GateResult(message, 1)

    if not reaction_history_is_settled:
        return fail_closed(
            "Agent Review blocked because an earlier YAGA request has no trusted success; "
            "open a fresh PR"
        )
    if reaction_request is None and _unsolicited_activity_is_visible(
        api,
        repository=repository,
        candidate=candidate,
    ):
        return fail_closed(
            "Agent Review blocked because connector activity has no exact YAGA request"
        )
    if reaction_request is not None and not _request_follows_authorization(
        reaction_request,
        authorization,
    ):
        return fail_closed(
            "Agent Review blocked because its request does not follow protected approval"
        )
    outcome = _current_outcome(
        api,
        repository=repository,
        candidate=candidate,
        reaction_request=reaction_request,
    )
    if (owner_authored or authorization is not None) and outcome is not None:
        if _settle_codex(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            lease=lease,
            success=True,
            outcome=outcome,
            authorization=authorization,
            reaction_request=reaction_request,
            request_key=key,
        ):
            return GateResult("success: Codex already reviewed the exact head", 0, "done")
        return GateResult("skipped: candidate changed while publishing success", 0)

    if owner_authored and reaction_request is not None:
        return GateResult("pending: Agent review is already in flight", 0, "observe")
    if not owner_authored and authorizations.approval is None:
        if reaction_request is not None:
            return fail_closed(
                "Agent Review blocked because the exact YAGA request lacks protected approval"
            )
        return GateResult(
            "pending: maintainer approval is required before requesting Codex",
            0,
            "external",
        )
    if not owner_authored and reaction_request is not None:
        return GateResult("pending: Agent review is already in flight", 0, "observe")
    route = "owner" if owner_authored else "approved"
    return GateResult("pending: a bounded Agent review request is needed", 0, route)


def authorize(
    api: RestApi,
    *,
    repository: str,
    source: SourceRun,
    lifecycle_workflow: str,
    server_url: str,
    direct_author_id: int,
) -> GateResult:
    """Record protected-environment approval without requesting a review."""
    if source.conclusion != "success":
        return GateResult("skipped: source CI did not succeed", 0)
    candidate = _load_current(
        api,
        repository=repository,
        source=source,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
    )
    if candidate is None:
        return GateResult("skipped: source candidate is no longer active", 0)
    direct_author_id = positive_int(direct_author_id, "direct review author ID")
    if candidate.author_id == direct_author_id:
        raise GateError("owner-authored pull requests do not use external approval")
    if not _revalidate(
        api,
        repository=repository,
        source=source,
        candidate=candidate,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
    ):
        return GateResult("skipped: source candidate changed before approval", 0)
    key = _request_key(repository, source, candidate)
    ensure_authorization(
        api,
        key=key,
        kind="approval",
        before_post=lambda: _revalidate(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
        ),
    )
    return GateResult("success: protected maintainer approval recorded", 0, "done")


def review(
    api: RestApi,
    *,
    repository: str,
    source: SourceRun,
    lifecycle_workflow: str,
    server_url: str,
    target_url: str,
    allow_request: bool,
    direct_author_id: int,
    poll_deadline: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> GateResult:
    """Optionally request once, then poll a bounded exact-head outcome."""
    if source.conclusion != "success":
        return GateResult("skipped: source CI did not succeed", 0)
    candidate = _load_current(
        api,
        repository=repository,
        source=source,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
    )
    if candidate is None:
        return GateResult("skipped: source candidate is no longer active", 0)
    if not _revalidate(
        api,
        repository=repository,
        source=source,
        candidate=candidate,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
    ):
        return GateResult("skipped: source candidate changed before publication", 0)
    lease = _publish_codex_pending(
        api,
        repository=repository,
        candidate=candidate,
        target_url=target_url,
    )
    key = _request_key(repository, source, candidate)
    authorizations = find_authorization(api, key=key)
    owner_authored = candidate.author_id == positive_int(
        direct_author_id,
        "direct review author ID",
    )
    authorization = None if owner_authored else authorizations.approval
    reaction_request = authorizations.request
    reaction_history_is_settled = _prior_requests_are_settled(
        api,
        repository=repository,
        requests=authorizations.prior_requests,
    )
    if not owner_authored and authorization is None:
        raise GateError("external Agent review request lacks protected approval")
    if not reaction_history_is_settled:
        _settle_codex(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            lease=lease,
            success=False,
        )
        return GateResult(
            "Agent Review blocked because an earlier YAGA request has no trusted success; "
            "open a fresh PR",
            1,
        )
    if reaction_request is None and _unsolicited_activity_is_visible(
        api,
        repository=repository,
        candidate=candidate,
    ):
        _settle_codex(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            lease=lease,
            success=False,
        )
        return GateResult(
            "Agent Review blocked because connector activity has no exact YAGA request",
            1,
        )
    if not allow_request and reaction_request is None:
        _settle_codex(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            lease=lease,
            success=False,
        )
        return GateResult("Agent Review blocked because its exact YAGA request is missing", 1)
    if allow_request and reaction_request is None:
        if not _revalidate(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            lease=lease,
        ):
            return GateResult("skipped: candidate changed before Codex authorization", 0)

        def request_is_still_needed() -> bool:
            if not _revalidate(
                api,
                repository=repository,
                source=source,
                candidate=candidate,
                lifecycle_workflow=lifecycle_workflow,
                server_url=server_url,
                lease=lease,
            ):
                return False
            latest_authorizations = find_authorization(api, key=key)
            if not _prior_requests_are_settled(
                api,
                repository=repository,
                requests=latest_authorizations.prior_requests,
            ):
                return False
            if not owner_authored:
                latest_approval = latest_authorizations.approval
                if latest_approval is None or not _authorization_capability_is_current(
                    api,
                    key=key,
                    authorization=latest_approval,
                ):
                    return False
            if latest_authorizations.request is not None:
                raise _RequestAlreadyCoveredError
            if _unsolicited_activity_is_visible(
                api,
                repository=repository,
                candidate=candidate,
            ):
                raise _UnsolicitedCodexActivityError
            return True

        try:
            reaction_request = ensure_authorization(
                api,
                key=key,
                kind="request",
                before_post=request_is_still_needed,
            )
        except _RequestAlreadyCoveredError:
            reaction_request = find_authorization(api, key=key).request
        except _UnsolicitedCodexActivityError:
            _settle_codex(
                api,
                repository=repository,
                source=source,
                candidate=candidate,
                lifecycle_workflow=lifecycle_workflow,
                server_url=server_url,
                lease=lease,
                success=False,
            )
            return GateResult(
                "Agent Review blocked because connector activity appeared before its request",
                1,
            )

    if reaction_request is None:
        _settle_codex(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            lease=lease,
            success=False,
        )
        return GateResult("Agent Review blocked because its exact YAGA request disappeared", 1)
    if not _request_follows_authorization(reaction_request, authorization):
        _settle_codex(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            lease=lease,
            success=False,
        )
        return GateResult(
            "Agent Review blocked because its request does not follow protected approval",
            1,
        )

    while True:
        try:
            require_success_tail(
                api,
                POLL_ITERATION_REQUEST_RESERVE,
                cleanup_margin_seconds=SUCCESS_CLEANUP_MARGIN_SECONDS,
            )
        except GateError:
            # The previous admitted iteration leaves this fixed error tail.
            require_success_tail(
                api,
                ERROR_WRITE_REQUEST_RESERVE,
                cleanup_margin_seconds=SUCCESS_CLEANUP_MARGIN_SECONDS,
            )
            _settle_codex(
                api,
                repository=repository,
                source=source,
                candidate=candidate,
                lifecycle_workflow=lifecycle_workflow,
                server_url=server_url,
                lease=lease,
                success=False,
            )
            return GateResult(TIMEOUT_DESCRIPTION, 1)
        if not _revalidate(
            api,
            repository=repository,
            source=source,
            candidate=candidate,
            lifecycle_workflow=lifecycle_workflow,
            server_url=server_url,
            lease=lease,
        ):
            return GateResult("skipped: candidate changed while waiting for Codex", 0)
        outcome = _current_outcome(
            api,
            repository=repository,
            candidate=candidate,
            reaction_request=reaction_request,
        )
        if outcome is not None:
            if _settle_codex(
                api,
                repository=repository,
                source=source,
                candidate=candidate,
                lifecycle_workflow=lifecycle_workflow,
                server_url=server_url,
                lease=lease,
                success=True,
                outcome=outcome,
                authorization=authorization,
                reaction_request=reaction_request,
                request_key=key,
            ):
                return GateResult("success: trusted exact-head Agent review is current", 0, "done")
            return GateResult("skipped: candidate changed while publishing success", 0)
        remaining = poll_deadline - clock()
        if remaining <= 0:
            _settle_codex(
                api,
                repository=repository,
                source=source,
                candidate=candidate,
                lifecycle_workflow=lifecycle_workflow,
                server_url=server_url,
                lease=lease,
                success=False,
            )
            return GateResult(TIMEOUT_DESCRIPTION, 1)
        sleep(min(float(POLL_INTERVAL_SECONDS), remaining))


def finalize(
    api: RestApi,
    *,
    repository: str,
    source: SourceRun,
    lifecycle_workflow: str,
    server_url: str,
    target_url: str,
    direct_author_id: int,
) -> GateResult:
    """Independently authorize and publish the final classic CI Gate status."""
    candidate = _load_current(
        api,
        repository=repository,
        source=source,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
    )
    if candidate is None:
        return GateResult("skipped: source candidate is no longer active", 0)
    codex = latest_trusted_status(
        api,
        repository=repository,
        head_sha=candidate.pull_request.head_sha,
        context=CODEX_STATUS_CONTEXT,
    )
    parsed = (
        parse_boundary_description(codex, head_sha=candidate.pull_request.head_sha)
        if codex is not None
        else None
    )
    codex_lineage = bool(
        codex is not None
        and terminal_has_pending_lineage(
            api,
            repository=repository,
            head_sha=candidate.pull_request.head_sha,
            terminal_status=codex,
            pending_description=boundary_description(candidate.boundary, "Codex pending"),
        )
    )
    owner_authored = candidate.author_id == positive_int(
        direct_author_id,
        "direct review author ID",
    )
    key = _request_key(repository, source, candidate)
    authorizations = find_authorization(api, key=key)
    authorization = None if owner_authored else authorizations.approval
    reaction_request = authorizations.request
    reaction_history_is_settled = _prior_requests_are_settled(
        api,
        repository=repository,
        requests=authorizations.prior_requests,
    )
    outcome = (
        _current_outcome(
            api,
            repository=repository,
            candidate=candidate,
            reaction_request=reaction_request,
        )
        if source.conclusion == "success"
        and codex is not None
        and codex.state == "success"
        and reaction_request is not None
        and _request_follows_authorization(reaction_request, authorization)
        and reaction_history_is_settled
        and parsed is not None
        and parsed[1] == "Codex passed"
        and same_boundary(parsed[0], candidate.boundary)
        and codex_lineage
        and (owner_authored or authorization is not None)
        else None
    )
    review_passed = bool(
        source.conclusion == "success"
        and codex is not None
        and codex.state == "success"
        and reaction_request is not None
        and _request_follows_authorization(reaction_request, authorization)
        and reaction_history_is_settled
        and parsed is not None
        and parsed[1] == "Codex passed"
        and same_boundary(parsed[0], candidate.boundary)
        and codex_lineage
        and (owner_authored or authorization is not None)
        and outcome is not None
    )
    settled = _settle_ci(
        api,
        repository=repository,
        source=source,
        candidate=candidate,
        lifecycle_workflow=lifecycle_workflow,
        server_url=server_url,
        target_url=target_url,
        success=review_passed,
        codex_status=codex if review_passed else None,
        outcome=outcome if review_passed else None,
        authorization=authorization if review_passed else None,
        reaction_request=reaction_request if review_passed else None,
        request_key=key if review_passed else None,
    )
    if not settled:
        return GateResult("skipped: candidate changed during CI Gate publication", 0)
    if review_passed:
        return GateResult("success: CI prerequisites and Agent review passed", 0, "done")
    return GateResult("CI Gate failed because Agent Review is not successful", 1)
