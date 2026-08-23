"""Tests for trusted Codex lifecycle source provenance."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta

import pytest

from tests.codex_support import (
    BASE,
    BASE_REF,
    BOUNDARY_AT,
    HEAD,
    PULL_REQUEST,
    REPOSITORY,
    RUN_ID,
    SERVER_URL,
    FakeApi,
    _boundary,
    _source_run,
    _status,
)
from yaga.codex import constants, provenance, statuses
from yaga.errors import GateError


def test_source_history_fails_closed_before_exhausting_the_rest_budget() -> None:
    valid = _boundary(run_id=RUN_ID, occurred_at=BOUNDARY_AT)
    claimed = [
        _boundary(
            run_id=RUN_ID + index + 1,
            occurred_at=BOUNDARY_AT + timedelta(seconds=index + 1),
        )
        for index in range(constants.MAX_SOURCE_BOUNDARY_VALIDATIONS + 1)
    ]
    source_runs = {valid.workflow_run_id: _source_run(valid)}
    for boundary in claimed:
        different = _boundary(
            run_id=boundary.workflow_run_id,
            occurred_at=boundary.occurred_at,
            base="c" * 40,
        )
        source_runs[boundary.workflow_run_id] = _source_run(different)
    api = FakeApi(
        statuses=[
            _status(valid, status_id=30_000),
            *[
                _status(boundary, status_id=30_001 + index)
                for index, boundary in enumerate(claimed)
            ],
        ],
        source_runs=source_runs,
    )
    usage = statuses.status_context_usage(api, repository=REPOSITORY, head_sha=HEAD)

    with pytest.raises(GateError, match="validation budget"):
        statuses.authoritative_status(
            api,
            usage,
            server_url=SERVER_URL,
            repository=REPOSITORY,
            pull_request_number=PULL_REQUEST,
            head_sha=HEAD,
            base_sha=BASE,
            base_ref=BASE_REF,
        )

    assert len([path for path in api.reads if "/actions/runs/" in path]) == (
        constants.MAX_SOURCE_BOUNDARY_VALIDATIONS
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda run: run.update(path=".github/workflows/maintainer-approval.yml"),
        lambda run: run.update(event="pull_request_review"),
        lambda run: run["repository"].update(full_name="attacker/repository"),
        lambda run: run.update(head_sha="c" * 40),
        lambda run: run.update(display_title="not-json"),
        lambda run: run.update(
            display_title=json.dumps(
                {
                    **json.loads(str(run["display_title"])),
                    "action": "edited",
                    "base_changed": False,
                }
            )
        ),
    ],
)
def test_source_boundary_rejects_wrong_path_event_repository_head_or_title(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    record = _source_run()
    mutate(record)
    api = FakeApi(source_runs={RUN_ID: record})
    with pytest.raises(GateError):
        provenance.source_boundary(api, repository=REPOSITORY, run_id=RUN_ID)


def test_source_boundary_accepts_base_edit_and_rejects_future_event_time() -> None:
    edited = _source_run(action="edited", base_changed=True)
    api = FakeApi(source_runs={RUN_ID: edited})
    assert provenance.source_boundary(api, repository=REPOSITORY, run_id=RUN_ID) == _boundary()
    assert provenance.source_boundary_and_action(
        api,
        repository=REPOSITORY,
        run_id=RUN_ID,
    ) == (_boundary(), "edited")

    future = _boundary(occurred_at=BOUNDARY_AT + timedelta(minutes=5))
    api = FakeApi(
        source_runs={RUN_ID: _source_run(future, created_at=BOUNDARY_AT + timedelta(minutes=1))}
    )
    with pytest.raises(GateError, match="postdates"):
        provenance.source_boundary(api, repository=REPOSITORY, run_id=RUN_ID)


def test_pull_request_target_rest_run_head_is_the_pr_head_not_the_base() -> None:
    record = _source_run()
    assert record["head_sha"] == "a" * 40
    assert record["head_sha"] != "b" * 40

    api = FakeApi(source_runs={RUN_ID: record})
    assert provenance.source_boundary(api, repository=REPOSITORY, run_id=RUN_ID) == _boundary()


def test_recovery_history_rejects_a_run_from_another_workflow_path() -> None:
    untrusted = _source_run(path=".github/workflows/untrusted.yml")
    api = FakeApi(
        workflow_history={"total_count": 1, "workflow_runs": [untrusted]},
    )

    with pytest.raises(GateError, match="not trusted"):
        provenance.latest_lifecycle_history(
            api,
            repository=REPOSITORY,
            head_sha=HEAD,
            not_before=BOUNDARY_AT - timedelta(minutes=1),
        )
