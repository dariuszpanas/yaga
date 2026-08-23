"""Resolve exact Codex candidates, head ownership, and bounded recovery work."""

from __future__ import annotations

from dataclasses import dataclass

from yaga.codex.evidence import connector_actor
from yaga.codex.models import Candidate
from yaga.codex.provenance import (
    LifecycleSource,
    ReviewBoundary,
    event_lifecycle_source,
    observer_workflow_path,
)
from yaga.errors import GateError
from yaga.github import RestApi, load_default_branch, load_pull_request
from yaga.models import (
    PullRequest,
    commit_sha,
    positive_int,
    record,
    repository_name,
)


@dataclass(frozen=True)
class HeadOwnership:
    """Current open-owner count and the live PR when ownership is unique."""

    open_count: int
    pull_request: PullRequest | None


def head_ownership(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
) -> HeadOwnership:
    """Resolve the unique live pull request that owns an exact head SHA."""
    records = api.paginate(f"/repos/{repository}/commits/{head_sha}/pulls")
    open_numbers: list[int] = []
    seen_numbers: set[int] = set()
    for index, item in enumerate(records):
        association = record(item, f"pull request associated with Codex head {index}")
        state = association.get("state")
        if state not in {"closed", "open"}:
            raise GateError("pull request associated with Codex head has an invalid state")
        number = positive_int(association.get("number"), "associated pull request number")
        if number in seen_numbers:
            raise GateError("pull request associated with Codex head is repeated")
        seen_numbers.add(number)
        head = record(association.get("head"), "associated pull request head")
        associated_head = commit_sha(head.get("sha"), "associated pull request head SHA")
        if associated_head == head_sha and state == "open":
            open_numbers.append(number)
    if not open_numbers or len(open_numbers) > 1:
        # Ambiguous ownership is a candidate-local fail-closed condition, not a
        # malformed discovery response. Scheduled reconciliation must still be
        # able to process unrelated heads in the same bounded pass.
        return HeadOwnership(open_count=len(open_numbers), pull_request=None)
    pull_request = load_pull_request(api, repository, open_numbers[0])
    if pull_request.state != "open" or pull_request.head_sha != head_sha:
        raise GateError("associated pull request no longer owns the Codex review head")
    return HeadOwnership(open_count=1, pull_request=pull_request)


def connector_review_workflow_candidate(
    api: RestApi,
    *,
    repository: str,
    event: dict[str, object],
) -> Candidate | None:
    """Resolve one trusted connector-review observer run to its live head owner."""
    repository = repository_name(repository)
    event_repository = record(event.get("repository"), "workflow event repository")
    if event_repository.get("full_name") != repository:
        raise GateError("workflow event belongs to another repository")
    event_run = record(event.get("workflow_run"), "review observer workflow run")
    run_id = positive_int(event_run.get("id"), "review observer workflow run ID")
    event_head = commit_sha(event_run.get("head_sha"), "review observer workflow head")
    if (
        event_run.get("path") != observer_workflow_path()
        or event_run.get("event") != "pull_request_review"
        or event_run.get("status") != "completed"
        or event_run.get("conclusion") != "success"
    ):
        raise GateError("review observer workflow run is not trusted")
    connector_actor(event_run.get("actor"), "review observer workflow actor")

    source = record(
        api.get(f"/repos/{repository}/actions/runs/{run_id}"),
        "review observer workflow run",
    )
    source_repository = record(source.get("repository"), "review observer workflow repository")
    source_head = commit_sha(source.get("head_sha"), "review observer workflow head")
    if (
        positive_int(source.get("id"), "review observer workflow run ID") != run_id
        or source.get("path") != observer_workflow_path()
        or source.get("event") != "pull_request_review"
        or source.get("status") != "completed"
        or source.get("conclusion") != "success"
        or source_repository.get("full_name") != repository
        or source_head != event_head
    ):
        raise GateError("review observer workflow source changed")
    connector_actor(source.get("actor"), "review observer workflow actor")
    connector_actor(source.get("triggering_actor"), "review observer triggering actor")

    ownership = head_ownership(api, repository=repository, head_sha=source_head)
    return (
        None
        if ownership.pull_request is None
        else candidate_from_pull_request(ownership.pull_request)
    )


def same_candidate(before: PullRequest, after: PullRequest | None) -> bool:
    """Return whether two live reads describe the same exact candidate."""
    return after is not None and (
        after.number,
        after.head_sha,
        after.head_ref,
        after.head_repository,
        after.base_ref,
        after.base_sha,
        after.draft,
        after.state,
    ) == (
        before.number,
        before.head_sha,
        before.head_ref,
        before.head_repository,
        before.base_ref,
        before.base_sha,
        before.draft,
        before.state,
    )


def candidate_from_pull_request(
    pull_request: PullRequest,
) -> Candidate:
    """Capture one exact pull-request candidate for serialized reconciliation."""
    return Candidate(
        pull_request_number=pull_request.number,
        head_sha=pull_request.head_sha,
        base_sha=pull_request.base_sha,
        base_ref=pull_request.base_ref,
        head_repository=pull_request.head_repository,
        head_ref=pull_request.head_ref,
    )


def wake_candidate(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
) -> Candidate | None:
    """Resolve one trusted wake to its unique live default-branch candidate."""
    repository = repository_name(repository)
    pull_request = load_pull_request(api, repository, pull_request_number)
    if (
        pull_request.state != "open"
        or pull_request.draft
        or pull_request.base_ref != load_default_branch(api, repository)
    ):
        return None
    ownership = head_ownership(api, repository=repository, head_sha=pull_request.head_sha)
    return (
        candidate_from_pull_request(pull_request)
        if same_candidate(pull_request, ownership.pull_request)
        else None
    )


def open_candidate(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_head: str,
    expected_base_sha: str,
    expected_base_ref: str,
    expected_head_repository: str,
    expected_head_ref: str,
) -> PullRequest | None:
    """Re-read one exact open candidate without assuming unique SHA ownership."""
    pull_request = load_pull_request(api, repository, pull_request_number)
    if pull_request.state != "open" or pull_request.draft:
        return None
    if (
        pull_request.head_sha != expected_head
        or pull_request.base_sha != expected_base_sha
        or pull_request.base_ref != expected_base_ref
        or pull_request.head_repository != expected_head_repository
        or pull_request.head_ref != expected_head_ref
    ):
        raise GateError("pull request candidate changed after invalidation")
    return pull_request


def open_recovery_candidate(
    api: RestApi,
    *,
    repository: str,
    candidate: Candidate,
) -> PullRequest | None:
    """Return a live recovery candidate while allowing base-only drift."""
    pull_request = load_pull_request(api, repository, candidate.pull_request_number)
    if pull_request.state != "open" or pull_request.draft:
        return None
    if (
        pull_request.head_sha != candidate.head_sha
        or pull_request.head_repository != candidate.head_repository
        or pull_request.head_ref != candidate.head_ref
    ):
        return None
    return pull_request


def open_owned_candidate(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_head: str,
    expected_base_sha: str,
    expected_base_ref: str,
    expected_head_repository: str,
    expected_head_ref: str,
) -> PullRequest | None:
    """Re-read an exact open candidate and require unique head ownership."""
    pull_request = open_candidate(
        api,
        repository=repository,
        pull_request_number=pull_request_number,
        expected_head=expected_head,
        expected_base_sha=expected_base_sha,
        expected_base_ref=expected_base_ref,
        expected_head_repository=expected_head_repository,
        expected_head_ref=expected_head_ref,
    )
    if pull_request is None:
        return None
    ownership = head_ownership(api, repository=repository, head_sha=pull_request.head_sha)
    if not same_candidate(pull_request, ownership.pull_request):
        raise GateError("Codex review head is not uniquely owned by the source pull request")
    return pull_request


def boundary_is_displaced(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    current_pull_request: PullRequest,
    boundary: ReviewBoundary,
) -> bool:
    """Return whether a trusted boundary no longer owns this commit."""
    if boundary.pull_request_number == current_pull_request.number:
        return (
            boundary.base_sha != current_pull_request.base_sha
            or boundary.base_ref != current_pull_request.base_ref
        )
    other = load_pull_request(api, repository, boundary.pull_request_number)
    return other.state != "open" or other.head_sha != head_sha


def event_lifecycle_candidate(
    api: RestApi,
    *,
    repository: str,
    event: dict[str, object],
) -> tuple[Candidate | None, LifecycleSource]:
    """Validate one observer completion and resolve its exact live candidate."""
    source, default_branch = event_lifecycle_source(
        api,
        repository=repository,
        event=event,
    )
    if source.action == "closed":
        return None, source

    pull_request = load_pull_request(api, repository, source.boundary.pull_request_number)
    live_default_branch = load_default_branch(api, repository)
    if (
        pull_request.head_sha != source.boundary.head_sha
        or pull_request.base_sha != source.boundary.base_sha
        or pull_request.base_ref != source.boundary.base_ref
        or pull_request.base_ref != default_branch
        or pull_request.base_ref != live_default_branch
    ):
        return None, source
    return candidate_from_pull_request(pull_request), source
