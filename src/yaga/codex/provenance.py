"""Validate Codex lifecycle boundary provenance and bounded source history."""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from yaga.codex.constants import (
    BOUNDARY_ACTIONS,
    DEFAULT_OBSERVER_WORKFLOW_PATH,
    MAX_DISPLAY_TITLE_BYTES,
    MAX_WORKFLOW_RUN_RECORDS,
    OBSERVER_ACTIONS,
    SOURCE_TITLE_KEYS,
)
from yaga.errors import GateError
from yaga.github import RestApi
from yaga.models import (
    MAX_GITHUB_ID,
    commit_sha,
    positive_int,
    record,
    ref_name,
    repository_name,
    timestamp,
    workflow_path,
)


@dataclass(frozen=True)
class ReviewBoundary:
    """Total-ordered lifecycle boundary persisted in an Actions status."""

    pull_request_number: int
    head_sha: str
    base_sha: str
    base_ref: str
    occurred_at: datetime
    workflow_run_id: int


@dataclass(frozen=True)
class LifecycleSource:
    """One lifecycle boundary encoded by the read-only observer workflow."""

    boundary: ReviewBoundary
    action: str
    base_changed: bool
    display_title: str


@dataclass(frozen=True)
class LifecycleHistory:
    """Newest bounded observer boundary found for one exact head."""

    source: LifecycleSource | None
    newest_source: LifecycleSource | None
    observer_source: LifecycleSource | None
    complete: bool
    observed_run_ids: frozenset[int]


def observer_workflow_path() -> str:
    """Return the validated repository-local unprivileged observer path."""
    return workflow_path(
        os.environ.get("YAGA_OBSERVER_WORKFLOW_PATH", DEFAULT_OBSERVER_WORKFLOW_PATH),
        "Codex review observer workflow path",
    )


def newer_boundary(left: ReviewBoundary | None, right: ReviewBoundary) -> ReviewBoundary:
    """Return the semantically newer of two lifecycle boundaries."""
    if left is None or (right.occurred_at, right.workflow_run_id) > (
        left.occurred_at,
        left.workflow_run_id,
    ):
        return right
    return left


def newer_ineligible_transition(
    history: LifecycleHistory,
    boundary: ReviewBoundary | None,
) -> LifecycleSource | None:
    """Return a later current-PR close/draft transition that cannot authorize success."""
    source = history.observer_source
    if source is None or source.action not in {"closed", "converted_to_draft"}:
        return None
    if boundary is not None and newer_boundary(boundary, source.boundary) == boundary:
        return None
    return source


def lifecycle_source_record(
    value: object,
    *,
    repository: str,
    expected_run_id: int | None = None,
) -> LifecycleSource:
    """Validate one observer workflow-run record returned by GitHub."""
    repository = repository_name(repository)
    payload = record(value, "Codex review lifecycle run")
    run_id = positive_int(payload.get("id"), "Codex review lifecycle run ID")
    if expected_run_id is not None and run_id != positive_int(
        expected_run_id, "expected Codex review lifecycle run ID"
    ):
        raise GateError("GitHub returned a different lifecycle run")
    if (
        payload.get("path") != observer_workflow_path()
        or payload.get("event") != "pull_request_target"
    ):
        raise GateError("Codex review boundary run is not trusted")
    if payload.get("status") != "completed":
        raise GateError("Codex review boundary run is not complete")
    run_repository = record(payload.get("repository"), "Codex review lifecycle repository")
    if run_repository.get("full_name") != repository:
        raise GateError("Codex review boundary run belongs to another repository")

    title_value = payload.get("display_title")
    if not isinstance(title_value, str):
        raise GateError("Codex review lifecycle title is invalid")
    try:
        title_bytes = title_value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise GateError("Codex review lifecycle title is invalid") from error
    if len(title_bytes) > MAX_DISPLAY_TITLE_BYTES:
        raise GateError("Codex review lifecycle title is invalid")
    try:
        title = record(json.loads(title_value), "Codex review lifecycle title")
    except json.JSONDecodeError as error:
        raise GateError("Codex review lifecycle title is invalid") from error
    if (
        set(title) != SOURCE_TITLE_KEYS
        or title.get("v") != 1
        or title.get("event") != "pull_request_target"
    ):
        raise GateError("Codex review lifecycle title fields are invalid")
    action = title.get("action")
    base_changed = title.get("base_changed")
    if (
        action not in OBSERVER_ACTIONS
        or not isinstance(base_changed, bool)
        or (action != "edited" and base_changed)
    ):
        raise GateError("Codex review lifecycle action is invalid")

    head_sha = commit_sha(title.get("head"), "Codex review lifecycle head")
    previous_head = commit_sha(title.get("previous"), "Codex review lifecycle previous head")
    if action != "synchronize" and previous_head != head_sha:
        raise GateError("Codex review lifecycle previous head is invalid for the action")

    boundary = ReviewBoundary(
        pull_request_number=positive_int(title.get("pr"), "Codex review lifecycle pull request"),
        head_sha=head_sha,
        base_sha=commit_sha(title.get("base"), "Codex review lifecycle base"),
        base_ref=ref_name(title.get("base_ref"), "Codex review lifecycle base ref"),
        occurred_at=timestamp(title.get("boundary"), "Codex review lifecycle boundary"),
        workflow_run_id=run_id,
    )
    # GITHUB_SHA for pull_request_target runners identifies trusted base code,
    # but the Actions REST workflow-run `head_sha` field identifies the PR head.
    # Live runs for PR #430 pin this distinction; do not compare this field to
    # boundary.base_sha.
    if payload.get("head_sha") != boundary.head_sha:
        raise GateError("Codex review boundary run identifies a different head")
    created_at = timestamp(payload.get("created_at"), "Codex review lifecycle run creation")
    if boundary.occurred_at > created_at:
        raise GateError("Codex review boundary postdates its workflow run")
    return LifecycleSource(
        boundary=boundary,
        action=cast(str, action),
        base_changed=base_changed,
        display_title=title_value,
    )


def lifecycle_source(
    api: RestApi,
    *,
    repository: str,
    run_id: int,
) -> LifecycleSource:
    """Load and validate one boundary encoded by the read-only observer."""
    run_id = positive_int(run_id, "Codex review lifecycle run ID")
    return lifecycle_source_record(
        api.get(f"/repos/{repository}/actions/runs/{run_id}"),
        repository=repository,
        expected_run_id=run_id,
    )


def event_lifecycle_source(
    api: RestApi,
    *,
    repository: str,
    event: dict[str, object],
) -> tuple[LifecycleSource, str]:
    """Validate one observer completion without reading the live pull request."""
    event_repository = record(event.get("repository"), "workflow event repository")
    if event_repository.get("full_name") != repository:
        raise GateError("workflow event belongs to another repository")
    default_branch = ref_name(event_repository.get("default_branch"), "default branch")
    event_run = record(event.get("workflow_run"), "Codex lifecycle workflow run")
    run_id = positive_int(event_run.get("id"), "Codex lifecycle workflow run ID")
    if (
        event_run.get("path") != observer_workflow_path()
        or event_run.get("event") != "pull_request_target"
        or event_run.get("status") != "completed"
    ):
        raise GateError("Codex lifecycle workflow event is not trusted")
    run_repository = record(event_run.get("repository"), "Codex lifecycle workflow repository")
    if run_repository.get("full_name") != repository:
        raise GateError("Codex lifecycle workflow belongs to another repository")

    source = lifecycle_source(api, repository=repository, run_id=run_id)
    if (
        event_run.get("head_sha") != source.boundary.head_sha
        or event_run.get("display_title") != source.display_title
    ):
        raise GateError("Codex lifecycle workflow event changed")
    return source, default_branch


def latest_lifecycle_history(
    api: RestApi,
    *,
    repository: str,
    head_sha: str,
    not_before: datetime,
    pull_request_number: int | None = None,
) -> LifecycleHistory:
    """Find the newest bounded observer boundary for one head.

    An incomplete first page is still useful for fail-closed invalidation, but
    must never authorize terminal success.
    """
    repository = repository_name(repository)
    head_sha = commit_sha(head_sha, "Codex lifecycle history head")
    if not_before.tzinfo is None or not_before.utcoffset() is None:
        raise GateError("Codex lifecycle history bound must include a timezone")
    not_before = not_before.astimezone(UTC)
    if pull_request_number is not None:
        pull_request_number = positive_int(
            pull_request_number,
            "Codex lifecycle history pull request",
        )
    created_bound = not_before.isoformat(timespec="seconds").replace("+00:00", "Z")
    workflow_file = observer_workflow_path().rsplit("/", 1)[1]
    query = urllib.parse.urlencode(
        {
            "created": f">={created_bound}",
            "event": "pull_request_target",
            "head_sha": head_sha,
            "page": "1",
            "per_page": str(MAX_WORKFLOW_RUN_RECORDS),
            "status": "completed",
        }
    )
    payload = record(
        api.get(f"/repos/{repository}/actions/workflows/{workflow_file}/runs?{query}"),
        "Codex review lifecycle history",
    )
    total_count = payload.get("total_count")
    records = payload.get("workflow_runs")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or not 0 <= total_count <= MAX_GITHUB_ID
        or not isinstance(records, list)
        or len(records) > MAX_WORKFLOW_RUN_RECORDS
        or len(records) != min(total_count, MAX_WORKFLOW_RUN_RECORDS)
    ):
        raise GateError("Codex review lifecycle history is incomplete or unbounded")

    seen_run_ids: set[int] = set()
    newest: LifecycleSource | None = None
    newest_for_pull_request: LifecycleSource | None = None
    newest_observer_for_pull_request: LifecycleSource | None = None
    for index, value in enumerate(records):
        run = record(value, f"Codex review lifecycle history record {index}")
        run_id = positive_int(run.get("id"), "Codex review lifecycle history run ID")
        if run_id in seen_run_ids:
            raise GateError("Codex review lifecycle history repeats a run")
        seen_run_ids.add(run_id)
        created_at = timestamp(run.get("created_at"), "Codex review lifecycle history creation")
        if created_at < not_before:
            raise GateError("Codex review lifecycle history ignored its creation bound")
        source = lifecycle_source_record(
            run,
            repository=repository,
            expected_run_id=run_id,
        )
        if source.boundary.head_sha != head_sha:
            raise GateError("Codex review lifecycle history returned a different head")
        if (
            pull_request_number is None
            or source.boundary.pull_request_number == pull_request_number
        ) and (
            newest_observer_for_pull_request is None
            or newer_boundary(
                newest_observer_for_pull_request.boundary,
                source.boundary,
            )
            == source.boundary
        ):
            newest_observer_for_pull_request = source
        if (
            source.action not in BOUNDARY_ACTIONS
            or (source.action == "edited") != source.base_changed
        ):
            continue
        if newest is None or newer_boundary(newest.boundary, source.boundary) == source.boundary:
            newest = source
        if (
            pull_request_number is None
            or source.boundary.pull_request_number == pull_request_number
        ) and (
            newest_for_pull_request is None
            or newer_boundary(newest_for_pull_request.boundary, source.boundary) == source.boundary
        ):
            newest_for_pull_request = source
    return LifecycleHistory(
        source=newest_for_pull_request,
        newest_source=newest,
        observer_source=newest_observer_for_pull_request,
        complete=total_count == len(records),
        observed_run_ids=frozenset(seen_run_ids),
    )


def source_boundary_and_action(
    api: RestApi,
    *,
    repository: str,
    run_id: int,
) -> tuple[ReviewBoundary, str]:
    """Load a lifecycle source and require an exact review boundary action."""
    source = lifecycle_source(api, repository=repository, run_id=run_id)
    if source.action not in BOUNDARY_ACTIONS or (source.action == "edited") != source.base_changed:
        raise GateError("Codex review lifecycle action is not a boundary")
    return source.boundary, source.action


def source_boundary(
    api: RestApi,
    *,
    repository: str,
    run_id: int,
) -> ReviewBoundary:
    """Return the trusted boundary from one observer workflow run."""
    boundary, _ = source_boundary_and_action(api, repository=repository, run_id=run_id)
    return boundary


def source_boundary_action(
    api: RestApi,
    *,
    repository: str,
    expected: ReviewBoundary,
) -> str:
    """Revalidate an expected boundary and return its lifecycle action."""
    boundary, action = source_boundary_and_action(
        api,
        repository=repository,
        run_id=expected.workflow_run_id,
    )
    if boundary != expected:
        raise GateError("Codex review boundary changed during evaluation")
    return action
