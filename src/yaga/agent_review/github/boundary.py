"""Authenticate the lifecycle boundary carried by YAGA commit statuses."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from yaga.agent_review.github.constants import (
    CODEX_STATUS_CONTEXT,
    EVENT_ACTIONS,
    GITHUB_ACTIONS_LOGIN,
    GITHUB_ACTIONS_USER_ID,
    MAX_WORKFLOW_RUN_RECORDS,
)
from yaga.errors import GateError
from yaga.github import RestApi
from yaga.models import (
    commit_sha,
    positive_int,
    record,
    repository_name,
    timestamp,
    workflow_path,
)
from yaga.status import CommitStatus

_ACTION_CODES = {
    "closed": "c",
    "converted_to_draft": "d",
    "edited": "e",
    "opened": "o",
    "ready_for_review": "y",
    "reopened": "r",
    "synchronize": "s",
}
_ACTIONS_BY_CODE = {code: action for action, code in _ACTION_CODES.items()}
_PHASE_CODES = {
    "CI pending": "CP",
    "Codex pending": "RP",
    "Codex passed": "RS",
    "Codex failed": "RF",
    "CI passed": "CS",
    "CI failed": "CF",
}
_PHASES_BY_CODE = {code: phase for phase, code in _PHASE_CODES.items()}
_DESCRIPTION_RE = re.compile(
    r"\AYAGA boundary (?P<run>[1-9][0-9]{0,18})/(?P<attempt>[1-9][0-9]{0,3}) "
    r"(?P<action>[cdeorys]) PR (?P<pr>[1-9][0-9]{0,18}) "
    r"base (?P<base>[0-9a-f]{40}) at (?P<time>[^\r\n]{10,40}) "
    r"(?P<phase>CP|RP|RS|RF|CS|CF)\Z"
)
_BOUNDARY_TITLE_RE = re.compile(
    r"\AYAGA (?P<action>closed|converted_to_draft|edited|opened|ready_for_review|reopened|synchronize) "
    r"boundary for #(?P<pr>[1-9][0-9]{0,18})\Z"
)
_METADATA_TITLE_RE = re.compile(r"\AYAGA metadata edit for #(?P<pr>[1-9][0-9]{0,18})\Z")


@dataclass(frozen=True)
class ReviewBoundary:
    """Exact trusted lifecycle transition shared by all publisher stages."""

    run_id: int
    run_attempt: int
    action: str
    pull_request_number: int
    head_sha: str
    base_sha: str
    occurred_at: datetime


def boundary_description(boundary: ReviewBoundary, phase: str) -> str:
    """Encode a human-readable, strictly parseable status description."""
    if phase not in _PHASE_CODES or boundary.action not in _ACTION_CODES:
        raise GateError("YAGA boundary phase is invalid")
    attempt = positive_int(boundary.run_attempt, "boundary attempt")
    if attempt > 9_999:
        raise GateError("YAGA boundary attempt is too large")
    occurred_at = boundary.occurred_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    description = (
        f"YAGA boundary {positive_int(boundary.run_id, 'boundary run')}/{attempt} "
        f"{_ACTION_CODES[boundary.action]} PR "
        f"{positive_int(boundary.pull_request_number, 'boundary PR')} "
        f"base {commit_sha(boundary.base_sha, 'boundary base')} at {occurred_at} "
        f"{_PHASE_CODES[phase]}"
    )
    if len(description) > 140:
        raise GateError("YAGA boundary description is too long")
    return description


def parse_boundary_description(
    status: CommitStatus,
    *,
    head_sha: str,
) -> tuple[ReviewBoundary, str] | None:
    """Parse an untrusted boundary claim from one status description."""
    if status.description is None:
        return None
    match = _DESCRIPTION_RE.fullmatch(status.description)
    if match is None:
        return None
    try:
        action = _ACTIONS_BY_CODE.get(match.group("action"))
        if action not in EVENT_ACTIONS:
            return None
        boundary = ReviewBoundary(
            run_id=positive_int(int(match.group("run")), "boundary run"),
            run_attempt=positive_int(int(match.group("attempt")), "boundary attempt"),
            action=action,
            pull_request_number=positive_int(int(match.group("pr")), "boundary PR"),
            head_sha=commit_sha(head_sha, "boundary head"),
            base_sha=commit_sha(match.group("base"), "boundary base"),
            occurred_at=timestamp(match.group("time"), "boundary occurrence"),
        )
    except (ValueError, GateError):
        return None
    phase = _PHASES_BY_CODE.get(match.group("phase"))
    return (boundary, phase) if phase is not None else None


def _attempt_url(
    server_url: str,
    repository: str,
    run_id: int,
    run_attempt: int,
) -> str:
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
        raise GateError("GitHub server URL is invalid")
    return (
        f"{parsed.scheme}://{parsed.netloc}/{repository}/actions/runs/"
        f"{run_id}/attempts/{run_attempt}"
    )


def validate_boundary_source(
    api: RestApi,
    *,
    repository: str,
    lifecycle_workflow: str,
    server_url: str,
    boundary: ReviewBoundary,
    provenance_status: CommitStatus,
) -> datetime:
    """Require the pending provenance and return its trusted run update time."""
    repository = repository_name(repository)
    lifecycle_workflow = workflow_path(lifecycle_workflow, "lifecycle workflow")
    if (
        provenance_status.context != CODEX_STATUS_CONTEXT
        or provenance_status.state != "pending"
        or provenance_status.creator_id != GITHUB_ACTIONS_USER_ID
        or provenance_status.creator_login != GITHUB_ACTIONS_LOGIN
        or provenance_status.target_url
        != _attempt_url(
            server_url,
            repository,
            boundary.run_id,
            boundary.run_attempt,
        )
    ):
        raise GateError("lifecycle boundary status provenance is invalid")
    parsed = parse_boundary_description(provenance_status, head_sha=boundary.head_sha)
    if parsed is None or parsed != (boundary, "CI pending"):
        raise GateError("lifecycle boundary status does not carry the expected boundary")

    payload = record(
        api.get(f"/repos/{repository}/actions/runs/{boundary.run_id}"),
        "lifecycle workflow run",
    )
    run_repository = record(payload.get("repository"), "lifecycle workflow repository")
    if (
        payload.get("id") != boundary.run_id
        or payload.get("run_attempt") != boundary.run_attempt
        or payload.get("path") != lifecycle_workflow
        or payload.get("event") != "pull_request_target"
        or payload.get("status") != "completed"
        or payload.get("conclusion") != "success"
        or payload.get("head_sha") != boundary.head_sha
        or run_repository.get("full_name") != repository
        or payload.get("display_title")
        != f"YAGA {boundary.action} boundary for #{boundary.pull_request_number}"
    ):
        raise GateError("lifecycle boundary workflow run is not trusted")
    created_at = timestamp(payload.get("created_at"), "lifecycle workflow creation")
    updated_at = timestamp(payload.get("updated_at"), "lifecycle workflow update")
    if (
        updated_at < created_at
        or boundary.occurred_at > created_at
        or provenance_status.created_at < created_at
    ):
        raise GateError("lifecycle boundary timing is invalid")

    run_number = positive_int(payload.get("run_number"), "lifecycle workflow number")
    current_position = (run_number, boundary.run_attempt, boundary.run_id)
    query = urllib.parse.urlencode(
        {
            "event": "pull_request_target",
            "head_sha": boundary.head_sha,
            "page": "1",
            "per_page": str(MAX_WORKFLOW_RUN_RECORDS),
        }
    )
    filename = lifecycle_workflow.rsplit("/", 1)[1]
    listing = record(
        api.get(f"/repos/{repository}/actions/workflows/{filename}/runs?{query}"),
        "lifecycle workflow runs",
    )
    count = listing.get("total_count")
    runs = listing.get("workflow_runs")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or not isinstance(runs, list)
        or count != len(runs)
        or count >= MAX_WORKFLOW_RUN_RECORDS
    ):
        raise GateError("lifecycle workflow run list is incomplete or invalid")
    seen: set[int] = set()
    current_seen = False
    for index, value in enumerate(runs):
        item = record(value, f"lifecycle workflow run {index}")
        item_repository = record(
            item.get("repository"),
            f"lifecycle workflow run {index} repository",
        )
        run_id = positive_int(item.get("id"), f"lifecycle workflow run {index} ID")
        if run_id in seen:
            raise GateError("lifecycle workflow run ID is repeated")
        seen.add(run_id)
        if (
            item.get("path") != lifecycle_workflow
            or item.get("event") != "pull_request_target"
            or item.get("head_sha") != boundary.head_sha
            or item_repository.get("full_name") != repository
        ):
            raise GateError("lifecycle workflow run list contains an untrusted run")
        position = (
            positive_int(item.get("run_number"), f"lifecycle workflow run {index} number"),
            positive_int(item.get("run_attempt"), f"lifecycle workflow run {index} attempt"),
            run_id,
        )
        title = item.get("display_title")
        boundary_title = _BOUNDARY_TITLE_RE.fullmatch(title) if isinstance(title, str) else None
        metadata_title = _METADATA_TITLE_RE.fullmatch(title) if isinstance(title, str) else None
        title_pr = (
            boundary_title.group("pr")
            if boundary_title is not None
            else metadata_title.group("pr")
            if metadata_title is not None
            else None
        )
        if title_pr is None or positive_int(int(title_pr), "lifecycle title PR") != (
            boundary.pull_request_number
        ):
            raise GateError("lifecycle workflow run title is invalid")
        current_seen |= position == current_position
        if position > current_position and metadata_title is None:
            raise GateError("a newer lifecycle boundary exists for this head")
    if not current_seen:
        raise GateError("lifecycle boundary is outside the bounded run window")
    return updated_at


def same_boundary(left: ReviewBoundary, right: ReviewBoundary) -> bool:
    """Compare stable identity while ignoring the provenance status timestamp."""
    return (
        left.run_id,
        left.run_attempt,
        left.action,
        left.pull_request_number,
        left.head_sha,
        left.base_sha,
    ) == (
        right.run_id,
        right.run_attempt,
        right.action,
        right.pull_request_number,
        right.head_sha,
        right.base_sha,
    )
