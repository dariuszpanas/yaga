"""Encode and authenticate the authoritative Codex commit-status context."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from yaga.codex.constants import (
    GITHUB_ACTIONS_LOGIN,
    GITHUB_ACTIONS_USER_ID,
    MAX_SOURCE_BOUNDARY_VALIDATIONS,
    MAX_STATUS_DESCRIPTION_CHARS,
    MAX_STATUS_PAGE_RECORDS,
    STATUS_CONTEXT,
)
from yaga.codex.provenance import ReviewBoundary, source_boundary
from yaga.errors import GateError
from yaga.github import RestApi
from yaga.models import commit_sha, positive_int, timestamp
from yaga.status import CommitStatus, parse_commit_status

BOUNDARY_DESCRIPTION_RE = re.compile(
    r"\ACodex (?P<state>pending|passed); PR #(?P<pr>[1-9][0-9]{0,18}); "
    r"base (?P<base>[0-9a-f]{40}); after (?P<time>[^\r\n]{10,40}); "
    r"run (?P<run>[1-9][0-9]{0,18})\Z"
)
UNCERTAIN_PENDING_DESCRIPTION = (
    "Codex pending; scheduled repair could not prove a lifecycle boundary"
)
_STATUS_CONTEXT_CASEFOLD = STATUS_CONTEXT.casefold()


@dataclass(frozen=True)
class StatusBoundaryClaim:
    """Boundary fields that fit in and can be claimed by a commit status."""

    pull_request_number: int
    head_sha: str
    base_sha: str
    occurred_at: datetime
    workflow_run_id: int


@dataclass(frozen=True)
class StatusContextUsage:
    """Capacity and recency information for the authoritative context."""

    count: int
    latest: CommitStatus | None
    statuses: tuple[CommitStatus, ...]


def _in_status_namespace(context: str) -> bool:
    """Match GitHub's case-insensitive commit-status context namespace."""
    return context.casefold() == _STATUS_CONTEXT_CASEFOLD


def status_description(value: str) -> str:
    """Validate the bounded single-line Codex status description."""
    if not value or "\n" in value or "\r" in value or len(value) > MAX_STATUS_DESCRIPTION_CHARS:
        raise GateError("Codex review status description is invalid")
    return value


def boundary_description(boundary: ReviewBoundary, *, state: str) -> str:
    """Encode one lifecycle boundary in a commit status description."""
    if state not in {"pending", "success"}:
        raise GateError("Codex review boundary state is invalid")
    label = "pending" if state == "pending" else "passed"
    occurred_at = boundary.occurred_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    description = (
        f"Codex {label}; PR #{boundary.pull_request_number}; "
        f"base {boundary.base_sha}; after {occurred_at}; run {boundary.workflow_run_id}"
    )
    return status_description(description)


def status_boundary(status: CommitStatus, *, head_sha: str) -> StatusBoundaryClaim | None:
    """Parse a status boundary claim without treating it as trusted evidence."""
    if status.description is None:
        return None
    match = BOUNDARY_DESCRIPTION_RE.fullmatch(status.description)
    if match is None:
        return None
    label = match.group("state")
    if (status.state, label) not in {("pending", "pending"), ("success", "passed")}:
        return None
    try:
        return StatusBoundaryClaim(
            pull_request_number=positive_int(
                int(match.group("pr")), "Codex review boundary pull request"
            ),
            head_sha=head_sha,
            base_sha=commit_sha(match.group("base"), "Codex review boundary base"),
            occurred_at=timestamp(match.group("time"), "Codex review boundary timestamp"),
            workflow_run_id=positive_int(
                int(match.group("run")), "Codex review boundary workflow run"
            ),
        )
    except (ValueError, GateError):
        return None


def workflow_run_url(
    server_url: str,
    repository: str,
    workflow_run_id: int,
) -> str:
    """Build an exact workflow-run URL from a trusted GitHub origin."""
    workflow_run_id = positive_int(workflow_run_id, "workflow run ID")
    parsed = urllib.parse.urlsplit(server_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise GateError("GitHub server URL must be an HTTPS origin")
    return f"{parsed.scheme}://{parsed.netloc}/{repository}/actions/runs/{workflow_run_id}"


def _actions_run_id(
    status: CommitStatus,
    *,
    server_url: str,
    repository: str,
) -> int | None:
    """Parse one exact same-repository Actions target from an Actions-owned status."""
    if (
        status.creator_id != GITHUB_ACTIONS_USER_ID
        or status.creator_login != GITHUB_ACTIONS_LOGIN
        or status.target_url is None
    ):
        return None
    parsed_server = urllib.parse.urlsplit(server_url)
    parsed_target = urllib.parse.urlsplit(status.target_url)
    expected_prefix = f"/{repository}/actions/runs/"
    if (
        parsed_server.scheme != "https"
        or not parsed_server.netloc
        or parsed_server.username is not None
        or parsed_server.password is not None
        or parsed_server.path not in {"", "/"}
        or parsed_server.query
        or parsed_server.fragment
        or parsed_target.scheme != parsed_server.scheme
        or parsed_target.netloc != parsed_server.netloc
        or not parsed_target.path.startswith(expected_prefix)
        or parsed_target.query
        or parsed_target.fragment
    ):
        return None
    suffix = parsed_target.path.removeprefix(expected_prefix)
    if not suffix.isdigit():
        return None
    try:
        return positive_int(int(suffix), "workflow run ID")
    except (ValueError, GateError):
        return None


def uncertain_pending_status(
    status: CommitStatus | None,
    *,
    server_url: str,
    repository: str,
) -> bool:
    """Recognize YAGA's fail-closed schedule status without claiming a boundary."""
    return (
        status is not None
        and status.context == STATUS_CONTEXT
        and status.state == "pending"
        and status.description == UNCERTAIN_PENDING_DESCRIPTION
        and _actions_run_id(status, server_url=server_url, repository=repository) is not None
    )


def status_context_usage(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
) -> StatusContextUsage:
    """Read and order every status in the authoritative context."""
    records = api.paginate(f"/repos/{repository}/commits/{head_sha}/statuses")
    status_ids: set[int] = set()
    latest: CommitStatus | None = None
    parsed_statuses: list[CommitStatus] = []
    for index, item in enumerate(records):
        status = parse_commit_status(item, label=f"Codex review status {index}")
        if not _in_status_namespace(status.context):
            continue
        if status.status_id in status_ids:
            raise GateError("Codex review status ID is repeated")
        status_ids.add(status.status_id)
        parsed_statuses.append(status)
        if latest is None or (status.created_at, status.status_id) > (
            latest.created_at,
            latest.status_id,
        ):
            latest = status
    statuses = tuple(
        sorted(
            parsed_statuses,
            key=lambda item: (item.created_at, item.status_id),
            reverse=True,
        )
    )
    return StatusContextUsage(count=len(status_ids), latest=latest, statuses=statuses)


def latest_status_context_page(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
) -> CommitStatus | None:
    """Read the newest visible gate status from one bounded repair page."""
    payload = api.get(
        f"/repos/{repository}/commits/{head_sha}/statuses?per_page={MAX_STATUS_PAGE_RECORDS}&page=1"
    )
    if (
        not isinstance(payload, list)
        or len(payload) > MAX_STATUS_PAGE_RECORDS
        or not all(isinstance(item, dict) for item in payload)
    ):
        raise GateError("Codex review repair status page is invalid")
    status_ids: set[int] = set()
    latest: CommitStatus | None = None
    for index, item in enumerate(payload):
        status = parse_commit_status(item, label=f"Codex review repair status {index}")
        if status.status_id in status_ids:
            raise GateError("Codex review repair status ID is repeated")
        status_ids.add(status.status_id)
        if _in_status_namespace(status.context) and (
            latest is None
            or (status.created_at, status.status_id) > (latest.created_at, latest.status_id)
        ):
            latest = status
    return latest


def claimed_actions_boundary(
    status: CommitStatus,
    *,
    server_url: str,
    repository: str,
    head_sha: str,
) -> StatusBoundaryClaim | None:
    """Extract a boundary claim from a plausibly Actions-owned status."""
    if status.context != STATUS_CONTEXT:
        return None
    run_id = _actions_run_id(status, server_url=server_url, repository=repository)
    if run_id is None:
        return None
    boundary = status_boundary(status, head_sha=head_sha)
    if boundary is None or run_id != boundary.workflow_run_id:
        return None
    return boundary


def trusted_actions_status(
    api: RestApi,
    status: CommitStatus,
    *,
    server_url: str,
    repository: str,
    head_sha: str,
    boundary_cache: dict[int, ReviewBoundary | None],
    source_validation_limit: int = MAX_SOURCE_BOUNDARY_VALIDATIONS,
) -> ReviewBoundary | None:
    """Resolve a status claim back to its trusted observer source."""
    claim = claimed_actions_boundary(
        status,
        server_url=server_url,
        repository=repository,
        head_sha=head_sha,
    )
    if claim is None:
        return None
    run_id = claim.workflow_run_id
    if run_id not in boundary_cache:
        if len(boundary_cache) >= source_validation_limit:
            raise GateError("Codex review source-boundary validation budget is exhausted")
        boundary_cache[run_id] = source_boundary(api, repository=repository, run_id=run_id)
    source = boundary_cache[run_id]
    if source is None or (
        source.pull_request_number,
        source.head_sha,
        source.base_sha,
        source.occurred_at,
        source.workflow_run_id,
    ) != (
        claim.pull_request_number,
        claim.head_sha,
        claim.base_sha,
        claim.occurred_at,
        claim.workflow_run_id,
    ):
        return None
    return source


def authoritative_status(
    api: RestApi,
    usage: StatusContextUsage,
    *,
    server_url: str,
    repository: str,
    pull_request_number: int | None,
    head_sha: str,
    base_sha: str | None,
    base_ref: str | None,
    boundary_cache: dict[int, ReviewBoundary | None] | None = None,
    source_validation_limit: int = MAX_SOURCE_BOUNDARY_VALIDATIONS,
) -> tuple[CommitStatus, ReviewBoundary] | None:
    """Return the newest trusted boundary matching the optional candidate filters."""
    if boundary_cache is None:
        boundary_cache = {}
    candidates: list[tuple[datetime, int, datetime, int, CommitStatus, StatusBoundaryClaim]] = []
    for status in usage.statuses:
        boundary = claimed_actions_boundary(
            status,
            server_url=server_url,
            repository=repository,
            head_sha=head_sha,
        )
        if boundary is None:
            continue
        if (
            pull_request_number is not None and boundary.pull_request_number != pull_request_number
        ) or (base_sha is not None and boundary.base_sha != base_sha):
            continue
        candidates.append(
            (
                boundary.occurred_at,
                boundary.workflow_run_id,
                status.created_at,
                status.status_id,
                status,
                boundary,
            )
        )
    for *_, status, _claim in sorted(candidates, key=lambda item: item[:4], reverse=True):
        trusted = trusted_actions_status(
            api,
            status,
            server_url=server_url,
            repository=repository,
            head_sha=head_sha,
            boundary_cache=boundary_cache,
            source_validation_limit=source_validation_limit,
        )
        if trusted is not None and (base_ref is None or trusted.base_ref == base_ref):
            return status, trusted
    return None
