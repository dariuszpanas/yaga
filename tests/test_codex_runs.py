"""Tests for exact current pull-request CI source authority."""

from __future__ import annotations

import copy

import pytest

from tests.codex_support import (
    CI_WORKFLOW_PATH,
    HEAD,
    LIFECYCLE_WORKFLOW_PATH,
    PULL_REQUEST,
    REPOSITORY,
    RUN_ATTEMPT,
    RUN_ID,
    RUN_NUMBER,
    FakeApi,
    _run,
)
from yaga.codex.runs import (
    SupersededRunError,
    load_source_from_wake,
    load_source_run,
    require_current_source,
)
from yaga.errors import GateError


def _source(
    *,
    run_id: int = RUN_ID,
    run_number: int = RUN_NUMBER,
) -> dict[str, object]:
    return _run(
        run_id=run_id,
        run_number=run_number,
        path=CI_WORKFLOW_PATH,
        event="pull_request",
        display_title=f"YAGA CI opened for #430 at base {'b' * 40}",
    )


def _event(source: dict[str, object]) -> dict[str, object]:
    return {
        "action": "completed",
        "repository": {"full_name": REPOSITORY, "default_branch": "main"},
        "workflow_run": copy.deepcopy(source),
    }


def _lifecycle_wake(
    *,
    display_title: str = f"YAGA opened boundary for #{PULL_REQUEST}",
    conclusion: str = "success",
) -> dict[str, object]:
    return _run(
        run_id=RUN_ID + 10,
        run_number=RUN_NUMBER + 10,
        path=LIFECYCLE_WORKFLOW_PATH,
        event="pull_request_target",
        display_title=display_title,
        conclusion=conclusion,
    )


def test_completed_exact_current_source_is_authoritative() -> None:
    source = _source()
    api = FakeApi(current_run=source, runs=[source])

    authority = load_source_run(
        api,
        repository=REPOSITORY,
        event_name="workflow_run",
        event=_event(source),
        workflow=CI_WORKFLOW_PATH,
    )

    assert authority.run_id == RUN_ID
    assert authority.run_attempt == RUN_ATTEMPT
    assert authority.head_sha == HEAD
    assert authority.conclusion == "success"
    require_current_source(api, repository=REPOSITORY, source=authority)


def test_lifecycle_completion_reconciles_ci_regardless_of_completion_order() -> None:
    lifecycle = _lifecycle_wake()
    completed = _source()
    api = FakeApi(current_run=lifecycle, runs=[lifecycle, completed])

    authority = load_source_from_wake(
        api,
        repository=REPOSITORY,
        event_name="workflow_run",
        event=_event(lifecycle),
        prerequisite_workflow=CI_WORKFLOW_PATH,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
    )

    assert authority is not None
    assert authority.run_id == RUN_ID

    running = _source(run_id=RUN_ID + 1, run_number=RUN_NUMBER + 1)
    running["status"] = "in_progress"
    running["conclusion"] = None
    waiting_api = FakeApi(current_run=lifecycle, runs=[lifecycle, running, completed])
    assert (
        load_source_from_wake(
            waiting_api,
            repository=REPOSITORY,
            event_name="workflow_run",
            event=_event(lifecycle),
            prerequisite_workflow=CI_WORKFLOW_PATH,
            lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        )
        is None
    )


@pytest.mark.parametrize(
    ("lifecycle", "source"),
    [
        (_lifecycle_wake(display_title=f"YAGA metadata edit for #{PULL_REQUEST}"), _source()),
        (_lifecycle_wake(conclusion="failure"), _source()),
        (_lifecycle_wake(), None),
    ],
)
def test_lifecycle_wakes_without_an_eligible_ci_source_are_noops(
    lifecycle: dict[str, object],
    source: dict[str, object] | None,
) -> None:
    runs = [lifecycle, *([source] if source is not None else [])]
    api = FakeApi(current_run=lifecycle, runs=runs)
    assert (
        load_source_from_wake(
            api,
            repository=REPOSITORY,
            event_name="workflow_run",
            event=_event(lifecycle),
            prerequisite_workflow=CI_WORKFLOW_PATH,
            lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        )
        is None
    )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("path", ".github/workflows/other.yml", "prerequisite"),
        ("event", "push", "prerequisite"),
        ("status", "in_progress", "prerequisite"),
        ("conclusion", None, "conclusion"),
        ("head_sha", "f" * 40, "changed"),
        ("run_attempt", RUN_ATTEMPT + 1, "changed"),
    ],
)
def test_source_event_or_refetch_drift_is_rejected(field: str, value: object, match: str) -> None:
    source = _source()
    event = _event(source)
    changed = copy.deepcopy(source)
    changed[field] = value
    if field in {"path", "event", "status", "conclusion"}:
        event["workflow_run"] = changed
        api = FakeApi(current_run=source, runs=[source])
    else:
        api = FakeApi(current_run=changed, runs=[changed])
    with pytest.raises(GateError, match=match):
        load_source_run(
            api,
            repository=REPOSITORY,
            event_name="workflow_run",
            event=event,
            workflow=CI_WORKFLOW_PATH,
        )


def test_newer_run_or_attempt_supersedes_source() -> None:
    source = _source()
    newer = _source(run_id=RUN_ID + 1, run_number=RUN_NUMBER + 1)
    api = FakeApi(current_run=source, runs=[newer, source])
    with pytest.raises(SupersededRunError, match="newer"):
        load_source_run(
            api,
            repository=REPOSITORY,
            event_name="workflow_run",
            event=_event(source),
            workflow=CI_WORKFLOW_PATH,
        )


def test_incomplete_first_page_never_authorizes_source() -> None:
    source = _source()

    class IncompleteApi(FakeApi):
        def get(self, path: str) -> object:
            value = super().get(path)
            if "/actions/workflows/ci.yml/runs?" in path:
                assert isinstance(value, dict)
                value["total_count"] = 2
            return value

    with pytest.raises(GateError, match="incomplete"):
        load_source_run(
            IncompleteApi(current_run=source, runs=[source]),
            repository=REPOSITORY,
            event_name="workflow_run",
            event=_event(source),
            workflow=CI_WORKFLOW_PATH,
        )
