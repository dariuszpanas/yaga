"""Publish attempt-owned Codex statuses and validate exact live ownership."""

from __future__ import annotations

from dataclasses import dataclass

from yaga.agent_review.github.constants import (
    CODEX_STATUS_CONTEXT,
    GITHUB_ACTIONS_LOGIN,
    GITHUB_ACTIONS_USER_ID,
    MAX_HEAD_ASSOCIATIONS,
    MAX_STATUS_DESCRIPTION_CHARS,
    MAX_STATUS_PAGE_RECORDS,
    MAX_STATUSES_PER_CONTEXT,
    PENDING_DESCRIPTION,
)
from yaga.errors import GateError
from yaga.github import RestApi, load_pull_request
from yaga.models import PullRequest, commit_sha, positive_int, record, repository_name
from yaga.status import CommitStatus, parse_commit_status


@dataclass(frozen=True)
class StatusLease:
    """Pending status uniquely owned by one workflow run attempt."""

    status_id: int
    head_sha: str
    target_url: str
    context: str = CODEX_STATUS_CONTEXT
    description: str = PENDING_DESCRIPTION


@dataclass(frozen=True)
class _GateStatusHistory:
    """Bounded case-insensitive gate-context history in GitHub's newest-first order."""

    statuses: tuple[CommitStatus, ...]
    visible_count: int


def _status_description(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_STATUS_DESCRIPTION_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GateError("Agent review status description is invalid")
    return value


def _actions_status(value: object, *, label: str) -> CommitStatus:
    status = parse_commit_status(value, label=label)
    if status.creator_id != GITHUB_ACTIONS_USER_ID or status.creator_login != GITHUB_ACTIONS_LOGIN:
        raise GateError(f"{label} has an unexpected creator")
    return status


def _publish_status(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    state: str,
    description: str,
    target_url: str,
    context: str = CODEX_STATUS_CONTEXT,
) -> CommitStatus:
    repository = repository_name(repository)
    head_sha = commit_sha(head_sha, "Agent review status head")
    description = _status_description(description)
    if state not in {"error", "pending", "success"}:
        raise GateError("Agent review publication state is invalid")
    payload: dict[str, object] = {
        "context": context,
        "description": description,
        "state": state,
        "target_url": target_url,
    }
    status = _actions_status(
        api.post(f"/repos/{repository}/statuses/{head_sha}", payload),
        label="published Agent review status",
    )
    if (
        status.state != state
        or status.context != context
        or status.description != description
        or status.target_url != target_url
    ):
        raise GateError("GitHub returned a different Agent review status")
    return status


def publish_pending(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    target_url: str,
    description: str = PENDING_DESCRIPTION,
    context: str = CODEX_STATUS_CONTEXT,
) -> StatusLease:
    """Create the attempt-specific pending lease before evidence reads."""
    status = _publish_status(
        api,
        repository=repository,
        head_sha=head_sha,
        state="pending",
        description=description,
        target_url=target_url,
        context=context,
    )
    return StatusLease(status.status_id, head_sha, target_url, context, description)


def repair_pending_best_effort(
    api: RestApi,
    *,
    repository: str,
    lease: StatusLease,
    target_url: str | None = None,
    description: str | None = None,
) -> bool:
    """Attempt pending without a probe; report only an exactly echoed repair."""
    try:
        publish_pending(
            api,
            repository=repository,
            head_sha=lease.head_sha,
            target_url=target_url or lease.target_url,
            context=lease.context,
            description=description or lease.description,
        )
    except GateError:
        return False
    return True


def _gate_status_history(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    context: str = CODEX_STATUS_CONTEXT,
) -> _GateStatusHistory:
    repository = repository_name(repository)
    head_sha = commit_sha(head_sha, "Agent review status head")
    payload = api.get(
        f"/repos/{repository}/commits/{head_sha}/statuses?per_page={MAX_STATUS_PAGE_RECORDS}&page=1"
    )
    if not isinstance(payload, list) or len(payload) >= MAX_STATUS_PAGE_RECORDS:
        raise GateError("Agent review status page is incomplete or invalid")
    statuses: list[CommitStatus] = []
    seen_ids: set[int] = set()
    previous_created_at = None
    for index, item in enumerate(payload):
        status = parse_commit_status(item, label=f"Agent review status {index}")
        if status.status_id in seen_ids:
            raise GateError("Agent review status page repeats a status")
        if previous_created_at is not None and status.created_at > previous_created_at:
            raise GateError("Agent review status page is not newest-first")
        seen_ids.add(status.status_id)
        previous_created_at = status.created_at
        if status.context.casefold() == context.casefold():
            statuses.append(status)
    return _GateStatusHistory(tuple(statuses), len(payload))


def ensure_status_capacity(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    contexts: tuple[str, ...],
    reserve_per_context: int,
) -> None:
    """Fail before a lifecycle write when a SHA is too close to GitHub's cap."""
    repository = repository_name(repository)
    head_sha = commit_sha(head_sha, "Agent review status capacity head")
    if (
        not contexts
        or len(set(contexts)) != len(contexts)
        or any(not isinstance(context, str) or not context for context in contexts)
    ):
        raise GateError("Agent review status capacity contexts are invalid")
    if (
        isinstance(reserve_per_context, bool)
        or not isinstance(reserve_per_context, int)
        or not 1 <= reserve_per_context < MAX_STATUSES_PER_CONTEXT
    ):
        raise GateError("Agent review status capacity reserve is invalid")

    counts = {context.casefold(): 0 for context in contexts}
    seen_ids: set[int] = set()
    previous_created_at = None
    for page in range(1, MAX_STATUSES_PER_CONTEXT // MAX_STATUS_PAGE_RECORDS + 1):
        payload = api.get(
            f"/repos/{repository}/commits/{head_sha}/statuses"
            f"?per_page={MAX_STATUS_PAGE_RECORDS}&page={page}"
        )
        if not isinstance(payload, list) or len(payload) > MAX_STATUS_PAGE_RECORDS:
            raise GateError("Agent review status capacity page is invalid")
        for index, item in enumerate(payload):
            status = parse_commit_status(
                item,
                label=f"Agent review status capacity page {page} item {index}",
            )
            if status.status_id in seen_ids:
                raise GateError("Agent review status capacity repeats a status")
            if previous_created_at is not None and status.created_at > previous_created_at:
                raise GateError("Agent review status capacity is not newest-first")
            seen_ids.add(status.status_id)
            previous_created_at = status.created_at
            normalized = status.context.casefold()
            if normalized in counts:
                counts[normalized] += 1
        if len(payload) < MAX_STATUS_PAGE_RECORDS:
            break
    else:
        raise GateError("commit status history reached GitHub's per-SHA scan ceiling")

    if any(count + reserve_per_context > MAX_STATUSES_PER_CONTEXT for count in counts.values()):
        raise GateError("commit status history lacks capacity for a complete YAGA boundary")


def status_history(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    context: str = CODEX_STATUS_CONTEXT,
) -> tuple[CommitStatus, ...]:
    """Return one bounded newest-first context history."""
    return _gate_status_history(
        api,
        repository=repository,
        head_sha=head_sha,
        context=context,
    ).statuses


def latest_trusted_status(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    context: str = CODEX_STATUS_CONTEXT,
) -> CommitStatus | None:
    """Return the latest exact Actions-owned status, rejecting collisions."""
    statuses = status_history(
        api,
        repository=repository,
        head_sha=head_sha,
        context=context,
    )
    if not statuses:
        return None
    latest = statuses[0]
    if not _trusted_actions_status(latest, context=context):
        raise GateError(f"latest {context} status is not trusted")
    return latest


def _trusted_actions_status(status: CommitStatus, *, context: str) -> bool:
    return bool(
        status.context == context
        and status.creator_id == GITHUB_ACTIONS_USER_ID
        and status.creator_login == GITHUB_ACTIONS_LOGIN
    )


def _status_matches_lease(status: CommitStatus, lease: StatusLease) -> bool:
    return bool(
        _trusted_actions_status(status, context=lease.context)
        and status.status_id == lease.status_id
        and status.state == "pending"
        and status.target_url == lease.target_url
        and status.description == lease.description
    )


def _status_matches_terminal(
    status: CommitStatus,
    *,
    lease: StatusLease,
    terminal_status: CommitStatus,
) -> bool:
    return bool(
        terminal_status.state in {"error", "success"}
        and _trusted_actions_status(status, context=lease.context)
        and status.status_id == terminal_status.status_id
        and status.state == terminal_status.state
        and status.target_url == lease.target_url
    )


def lease_is_current(
    api: RestApi,
    *,
    repository: str,
    lease: StatusLease,
) -> bool:
    """Require this attempt's pending to be physically latest in the context."""
    history = _gate_status_history(
        api,
        repository=repository,
        head_sha=lease.head_sha,
        context=lease.context,
    )
    return bool(history.statuses and _status_matches_lease(history.statuses[0], lease))


def publish_terminal(
    api: RestApi,
    *,
    repository: str,
    lease: StatusLease,
    state: str,
    description: str,
) -> CommitStatus | None:
    """Publish only while this attempt still owns the latest pending lease."""
    if state not in {"error", "success"}:
        raise GateError("Agent review terminal state is invalid")
    try:
        history = _gate_status_history(
            api,
            repository=repository,
            head_sha=lease.head_sha,
            context=lease.context,
        )
    except GateError:
        repair_pending_best_effort(api, repository=repository, lease=lease)
        raise
    latest = history.statuses[0] if history.statuses else None
    if latest is None or not _status_matches_lease(latest, lease):
        # Preserve a newer canonical Actions-owned result. A missing, malformed,
        # case-colliding, or same-ID mutation is uncertain and needs a fresh pending.
        if (
            latest is None
            or not _trusted_actions_status(latest, context=lease.context)
            or latest.status_id == lease.status_id
        ):
            repair_pending_best_effort(api, repository=repository, lease=lease)
        return None
    if history.visible_count > MAX_STATUS_PAGE_RECORDS - 2:
        # Keep the current pending lease authoritative. A terminal must remain
        # visible on a non-full page, with one final slot available for a
        # best-effort pending repair after a later validation race.
        raise GateError("commit status page lacks a terminal and repair slot")
    try:
        return _publish_status(
            api,
            repository=repository,
            head_sha=lease.head_sha,
            state=state,
            description=description,
            target_url=lease.target_url,
            context=lease.context,
        )
    except GateError:
        # The terminal POST may have been accepted even when its response was
        # lost or malformed. Repair without first spending another read.
        repair_pending_best_effort(api, repository=repository, lease=lease)
        raise


def terminal_is_current(
    api: RestApi,
    *,
    repository: str,
    lease: StatusLease,
    terminal_status: CommitStatus,
) -> bool:
    """Require this terminal latest with the exact lease immediately before it."""
    history = _gate_status_history(
        api,
        repository=repository,
        head_sha=lease.head_sha,
        context=lease.context,
    )
    if len(history.statuses) < 2:
        return False
    latest, predecessor = history.statuses[:2]
    return bool(
        _status_matches_terminal(
            latest,
            lease=lease,
            terminal_status=terminal_status,
        )
        and _status_matches_lease(predecessor, lease)
    )


def terminal_has_pending_lineage(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    terminal_status: CommitStatus,
    pending_description: str,
) -> bool:
    """Require the latest terminal to immediately follow its own pending lease."""
    history = _gate_status_history(
        api,
        repository=repository,
        head_sha=head_sha,
        context=terminal_status.context,
    )
    if len(history.statuses) < 2:
        return False
    latest, predecessor = history.statuses[:2]
    return bool(
        latest == terminal_status
        and latest.state in {"error", "success"}
        and _trusted_actions_status(latest, context=terminal_status.context)
        and latest.target_url is not None
        and _trusted_actions_status(predecessor, context=terminal_status.context)
        and predecessor.state == "pending"
        and predecessor.target_url == latest.target_url
        and predecessor.description == pending_description
    )


def restore_pending_after_race(
    api: RestApi,
    *,
    repository: str,
    lease: StatusLease,
    terminal_status: CommitStatus,
) -> bool:
    """Repair an uncertain terminal without displacing a newer trusted result."""
    try:
        history = _gate_status_history(
            api,
            repository=repository,
            head_sha=lease.head_sha,
            context=lease.context,
        )
    except GateError:
        return repair_pending_best_effort(api, repository=repository, lease=lease)

    latest = history.statuses[0] if history.statuses else None
    if latest is not None and _trusted_actions_status(latest, context=lease.context):
        if latest.status_id != terminal_status.status_id:
            return False
        if not _status_matches_terminal(
            latest,
            lease=lease,
            terminal_status=terminal_status,
        ):
            return repair_pending_best_effort(api, repository=repository, lease=lease)

        target_url = lease.target_url
        description = lease.description
        if len(history.statuses) >= 2:
            predecessor = history.statuses[1]
            if (
                _trusted_actions_status(predecessor, context=lease.context)
                and predecessor.state == "pending"
                and predecessor.status_id != lease.status_id
                and predecessor.target_url is not None
                and predecessor.description is not None
            ):
                target_url = predecessor.target_url
                description = predecessor.description
        return repair_pending_best_effort(
            api,
            repository=repository,
            lease=lease,
            target_url=target_url,
            description=description,
        )

    # No visible gate status, or a case-colliding/untrusted writer, is not
    # sufficient authority to preserve a possibly-successful physical tail.
    return repair_pending_best_effort(api, repository=repository, lease=lease)


def same_candidate(expected: PullRequest, actual: PullRequest) -> bool:
    """Compare every event-bound pull-request identity field."""
    return (
        expected.number == actual.number
        and expected.head_sha == actual.head_sha
        and expected.head_ref == actual.head_ref
        and expected.head_repository == actual.head_repository
        and expected.base_sha == actual.base_sha
        and expected.base_ref == actual.base_ref
    )


def load_exact_live_pull_request(
    api: RestApi,
    *,
    repository: str,
    expected: PullRequest,
    default_branch: str,
    require_ready: bool,
) -> PullRequest | None:
    """Return the unchanged live candidate, or ``None`` after a normal race."""
    live = load_pull_request(api, repository, expected.number)
    if (
        not same_candidate(expected, live)
        or live.state != "open"
        or live.base_ref != default_branch
        or (require_ready and live.draft)
    ):
        return None
    return live


def uniquely_owned(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    pull_request_number: int,
) -> bool:
    """Require one exact open PR owner without unbounded pagination."""
    repository = repository_name(repository)
    head_sha = commit_sha(head_sha, "Agent review head ownership")
    pull_request_number = positive_int(pull_request_number, "Agent review pull request")
    payload = api.get(
        f"/repos/{repository}/commits/{head_sha}/pulls?per_page={MAX_HEAD_ASSOCIATIONS}&page=1"
    )
    if not isinstance(payload, list) or len(payload) > MAX_HEAD_ASSOCIATIONS:
        raise GateError("Agent review head associations are invalid")
    if len(payload) == MAX_HEAD_ASSOCIATIONS:
        return False
    open_numbers: list[int] = []
    seen_numbers: set[int] = set()
    for index, item in enumerate(payload):
        association = record(item, f"Agent review head association {index}")
        state = association.get("state")
        if state not in {"closed", "open"}:
            raise GateError("Agent review head association state is invalid")
        number = positive_int(association.get("number"), "associated pull request number")
        if number in seen_numbers:
            raise GateError("Agent review head association is repeated")
        seen_numbers.add(number)
        head = record(association.get("head"), "associated pull request head")
        associated_sha = commit_sha(head.get("sha"), "associated pull request head SHA")
        if state == "open" and associated_sha == head_sha:
            open_numbers.append(number)
    return open_numbers == [pull_request_number]


def load_exact_unique_ready_pull_request(
    api: RestApi,
    *,
    repository: str,
    expected: PullRequest,
    default_branch: str,
) -> PullRequest | None:
    """Re-read exact ready state and unique SHA ownership."""
    live = load_exact_live_pull_request(
        api,
        repository=repository,
        expected=expected,
        default_branch=default_branch,
        require_ready=True,
    )
    if live is None or not uniquely_owned(
        api,
        repository=repository,
        head_sha=expected.head_sha,
        pull_request_number=expected.number,
    ):
        return None
    return live
