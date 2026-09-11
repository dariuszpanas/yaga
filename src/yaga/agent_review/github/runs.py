"""Validate the exact completed CI run that wakes the trusted publisher."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from yaga.agent_review.github.constants import MAX_WORKFLOW_RUN_RECORDS
from yaga.errors import GateError
from yaga.github import RestApi
from yaga.models import (
    commit_sha,
    positive_int,
    record,
    ref_name,
    repository_name,
    timestamp,
    workflow_path,
)


class SupersededRunError(GateError):
    """The source CI attempt is no longer the newest attempt for this head."""


@dataclass(frozen=True)
class _LifecycleWake:
    """One completed trusted lifecycle run that may reconcile with CI."""

    run_id: int
    run_attempt: int
    run_number: int
    head_sha: str
    conclusion: str
    action: str | None
    pull_request_number: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SourceRun:
    """Exact completed pull-request CI attempt permitted to wake YAGA."""

    run_id: int
    run_attempt: int
    run_number: int
    workflow_path: str
    head_sha: str
    conclusion: str
    created_at: datetime
    updated_at: datetime
    pull_request_number: int
    pull_request_action: str
    base_sha: str
    head_ref: str
    head_repository: str


_SOURCE_TITLE_RE = re.compile(
    r"\AYAGA CI (?P<action>opened|ready_for_review|reopened|synchronize) "
    r"for #(?P<pr>[1-9][0-9]{0,18}) at base (?P<base>[0-9a-f]{40})\Z"
)
_LIFECYCLE_TITLE_RE = re.compile(
    r"\AYAGA (?:(?P<action>converted_to_draft|edited|opened|ready_for_review|reopened|synchronize) "
    r"boundary|metadata edit) for #(?P<pr>[1-9][0-9]{0,18})\Z"
)


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise GateError(f"{label} must be a bounded nonnegative integer")
    return value


def _source_run(
    value: object,
    *,
    repository: str,
    workflow: str,
    label: str,
) -> SourceRun:
    payload = record(value, label)
    run_repository = record(payload.get("repository"), f"{label} repository")
    conclusion = payload.get("conclusion")
    if conclusion not in {
        "action_required",
        "cancelled",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "success",
        "timed_out",
    }:
        raise GateError(f"{label} conclusion is invalid")
    if (
        payload.get("path") != workflow
        or payload.get("event") != "pull_request"
        or payload.get("status") != "completed"
        or run_repository.get("full_name") != repository
    ):
        raise GateError(f"{label} is not a completed pull-request prerequisite run")
    title = payload.get("display_title")
    title_match = _SOURCE_TITLE_RE.fullmatch(title) if isinstance(title, str) else None
    if title_match is None:
        raise GateError(f"{label} title is not a bounded YAGA CI boundary")
    pull_request_number = positive_int(int(title_match.group("pr")), f"{label} title PR")
    head_sha = commit_sha(payload.get("head_sha"), f"{label} head")
    head_repository = record(payload.get("head_repository"), f"{label} head repository")
    created_at = timestamp(payload.get("created_at"), f"{label} creation")
    updated_at = timestamp(payload.get("updated_at"), f"{label} update")
    if updated_at < created_at:
        raise GateError(f"{label} update predates its creation")
    return SourceRun(
        run_id=positive_int(payload.get("id"), f"{label} ID"),
        run_attempt=positive_int(payload.get("run_attempt"), f"{label} attempt"),
        run_number=positive_int(payload.get("run_number"), f"{label} number"),
        workflow_path=workflow,
        head_sha=head_sha,
        conclusion=conclusion,
        created_at=created_at,
        updated_at=updated_at,
        pull_request_number=pull_request_number,
        pull_request_action=title_match.group("action"),
        base_sha=commit_sha(title_match.group("base"), f"{label} title base"),
        head_ref=ref_name(payload.get("head_branch"), f"{label} head branch"),
        head_repository=repository_name(head_repository.get("full_name")),
    )


def _position(run: SourceRun) -> tuple[int, int, int]:
    return run.run_number, run.run_attempt, run.run_id


def _lifecycle_wake(
    value: object,
    *,
    repository: str,
    workflow: str,
    label: str,
) -> _LifecycleWake:
    payload = record(value, label)
    run_repository = record(payload.get("repository"), f"{label} repository")
    conclusion = payload.get("conclusion")
    if conclusion not in {
        "action_required",
        "cancelled",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "success",
        "timed_out",
    }:
        raise GateError(f"{label} conclusion is invalid")
    if (
        payload.get("path") != workflow
        or payload.get("event") != "pull_request_target"
        or payload.get("status") != "completed"
        or run_repository.get("full_name") != repository
    ):
        raise GateError(f"{label} is not a completed lifecycle run")
    title = payload.get("display_title")
    match = _LIFECYCLE_TITLE_RE.fullmatch(title) if isinstance(title, str) else None
    if match is None:
        raise GateError(f"{label} title is not a bounded lifecycle wake")
    created_at = timestamp(payload.get("created_at"), f"{label} creation")
    updated_at = timestamp(payload.get("updated_at"), f"{label} update")
    if updated_at < created_at:
        raise GateError(f"{label} update predates its creation")
    return _LifecycleWake(
        run_id=positive_int(payload.get("id"), f"{label} ID"),
        run_attempt=positive_int(payload.get("run_attempt"), f"{label} attempt"),
        run_number=positive_int(payload.get("run_number"), f"{label} number"),
        head_sha=commit_sha(payload.get("head_sha"), f"{label} head"),
        conclusion=conclusion,
        action=match.group("action"),
        pull_request_number=positive_int(int(match.group("pr")), f"{label} title PR"),
        created_at=created_at,
        updated_at=updated_at,
    )


def _latest_completed_source_for_wake(
    api: RestApi,
    *,
    repository: str,
    workflow: str,
    wake: _LifecycleWake,
) -> SourceRun | None:
    query = urllib.parse.urlencode(
        {
            "event": "pull_request",
            "head_sha": wake.head_sha,
            "page": "1",
            "per_page": str(MAX_WORKFLOW_RUN_RECORDS),
        }
    )
    filename = workflow.rsplit("/", 1)[1]
    response = record(
        api.get(f"/repos/{repository}/actions/workflows/{filename}/runs?{query}"),
        "paired CI workflow runs",
    )
    total_count = _nonnegative_int(response.get("total_count"), "paired CI run count")
    records = response.get("workflow_runs")
    if (
        not isinstance(records, list)
        or total_count != len(records)
        or total_count >= MAX_WORKFLOW_RUN_RECORDS
    ):
        raise GateError("paired CI workflow run list is incomplete or invalid")
    positioned: list[tuple[tuple[int, int, int], dict[str, object]]] = []
    seen: set[int] = set()
    for index, value in enumerate(records):
        payload = record(value, f"paired CI workflow run {index}")
        run_repository = record(
            payload.get("repository"),
            f"paired CI workflow run {index} repository",
        )
        run_id = positive_int(payload.get("id"), f"paired CI workflow run {index} ID")
        if run_id in seen:
            raise GateError("paired CI workflow run ID is repeated")
        seen.add(run_id)
        if (
            payload.get("path") != workflow
            or payload.get("event") != "pull_request"
            or payload.get("head_sha") != wake.head_sha
            or run_repository.get("full_name") != repository
        ):
            raise GateError("paired CI workflow run list contains an untrusted run")
        positioned.append(
            (
                (
                    positive_int(
                        payload.get("run_number"),
                        f"paired CI workflow run {index} number",
                    ),
                    positive_int(
                        payload.get("run_attempt"),
                        f"paired CI workflow run {index} attempt",
                    ),
                    run_id,
                ),
                payload,
            )
        )
    if not positioned:
        return None
    _, latest = max(positioned, key=lambda item: item[0])
    if latest.get("status") != "completed":
        return None
    source = _source_run(
        latest,
        repository=repository,
        workflow=workflow,
        label="paired source CI run",
    )
    if (
        source.pull_request_number != wake.pull_request_number
        or source.pull_request_action != wake.action
    ):
        return None
    require_current_source(api, repository=repository, source=source)
    return source


def require_current_source(
    api: RestApi,
    *,
    repository: str,
    source: SourceRun,
) -> None:
    """Re-fetch the source and reject incomplete or superseded first pages."""
    repository = repository_name(repository)
    current = _source_run(
        api.get(f"/repos/{repository}/actions/runs/{source.run_id}"),
        repository=repository,
        workflow=source.workflow_path,
        label="source CI run",
    )
    if current != source:
        raise SupersededRunError("source CI run or attempt changed")

    query = urllib.parse.urlencode(
        {
            "event": "pull_request",
            "head_sha": source.head_sha,
            "page": "1",
            "per_page": str(MAX_WORKFLOW_RUN_RECORDS),
        }
    )
    filename = source.workflow_path.rsplit("/", 1)[1]
    response = record(
        api.get(f"/repos/{repository}/actions/workflows/{filename}/runs?{query}"),
        "source CI workflow runs",
    )
    total_count = _nonnegative_int(response.get("total_count"), "source CI run count")
    records = response.get("workflow_runs")
    if (
        not isinstance(records, list)
        or total_count != len(records)
        or total_count >= MAX_WORKFLOW_RUN_RECORDS
    ):
        raise GateError("source CI workflow run list is incomplete or invalid")
    runs = [
        _source_run(
            value,
            repository=repository,
            workflow=source.workflow_path,
            label=f"source CI workflow run {index}",
        )
        for index, value in enumerate(records)
    ]
    ids = [run.run_id for run in runs]
    if len(ids) != len(set(ids)):
        raise GateError("source CI workflow run ID is repeated")
    if source.run_id not in ids or any(_position(run) > _position(source) for run in runs):
        raise SupersededRunError("a newer source CI run exists for this head")


def load_source_run(
    api: RestApi,
    *,
    repository: str,
    event_name: str,
    event: dict[str, object],
    workflow: str,
) -> SourceRun:
    """Authenticate the workflow_run completion and its exact current source."""
    repository = repository_name(repository)
    workflow = workflow_path(workflow, "prerequisite workflow")
    if event_name != "workflow_run" or event.get("action") != "completed":
        raise GateError("Codex publisher requires a completed workflow_run event")
    event_repository = record(event.get("repository"), "workflow event repository")
    if event_repository.get("full_name") != repository:
        raise GateError("workflow event belongs to another repository")
    event_run = _source_run(
        event.get("workflow_run"),
        repository=repository,
        workflow=workflow,
        label="workflow event source CI run",
    )
    source = _source_run(
        api.get(f"/repos/{repository}/actions/runs/{event_run.run_id}"),
        repository=repository,
        workflow=workflow,
        label="source CI run",
    )
    if source != event_run:
        raise GateError("workflow event source CI run changed")
    require_current_source(api, repository=repository, source=source)
    return source


def load_source_from_wake(
    api: RestApi,
    *,
    repository: str,
    event_name: str,
    event: dict[str, object],
    prerequisite_workflow: str,
    lifecycle_workflow: str,
) -> SourceRun | None:
    """Resolve either CI completion order to the newest exact CI authority."""
    repository = repository_name(repository)
    prerequisite_workflow = workflow_path(
        prerequisite_workflow,
        "prerequisite workflow",
    )
    lifecycle_workflow = workflow_path(lifecycle_workflow, "lifecycle workflow")
    if event_name != "workflow_run" or event.get("action") != "completed":
        raise GateError("Codex publisher requires a completed workflow_run event")
    event_repository = record(event.get("repository"), "workflow event repository")
    if event_repository.get("full_name") != repository:
        raise GateError("workflow event belongs to another repository")
    event_run = record(event.get("workflow_run"), "workflow event run")
    path = event_run.get("path")
    if path == prerequisite_workflow:
        return load_source_run(
            api,
            repository=repository,
            event_name=event_name,
            event=event,
            workflow=prerequisite_workflow,
        )
    if path != lifecycle_workflow:
        raise GateError("workflow event is not an allowed YAGA wake")
    wake = _lifecycle_wake(
        event_run,
        repository=repository,
        workflow=lifecycle_workflow,
        label="workflow event lifecycle run",
    )
    current = _lifecycle_wake(
        api.get(f"/repos/{repository}/actions/runs/{wake.run_id}"),
        repository=repository,
        workflow=lifecycle_workflow,
        label="current lifecycle run",
    )
    if current != wake:
        raise GateError("workflow event lifecycle run changed")
    if wake.conclusion != "success" or wake.action is None:
        return None
    return _latest_completed_source_for_wake(
        api,
        repository=repository,
        workflow=prerequisite_workflow,
        wake=wake,
    )
