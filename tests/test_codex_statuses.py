"""Tests for authoritative Codex status parsing and selection."""

from __future__ import annotations

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
from yaga.codex import constants, statuses


def test_authoritative_history_validates_only_the_semantically_newest_source() -> None:
    boundaries = [
        _boundary(
            run_id=RUN_ID + index,
            occurred_at=BOUNDARY_AT + timedelta(seconds=index),
        )
        for index in range(400)
    ]
    newest = boundaries[-1]
    api = FakeApi(
        statuses=[
            _status(
                boundary,
                status_id=20_000 + index,
                created_at=boundary.occurred_at + timedelta(seconds=20),
            )
            for index, boundary in enumerate(boundaries)
        ],
        source_runs={newest.workflow_run_id: _source_run(newest)},
    )
    usage = statuses.status_context_usage(api, repository=REPOSITORY, head_sha=HEAD)

    status, boundary = statuses.authoritative_status(
        api,
        usage,
        server_url=SERVER_URL,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        head_sha=HEAD,
        base_sha=BASE,
        base_ref=BASE_REF,
    ) or pytest.fail("expected authoritative boundary")

    assert status.status_id == 20_399
    assert boundary == newest
    assert [path for path in api.reads if "/actions/runs/" in path] == [
        f"/repos/{REPOSITORY}/actions/runs/{newest.workflow_run_id}"
    ]


@pytest.mark.parametrize(
    "status",
    [
        _status(
            creator={
                "id": constants.GITHUB_ACTIONS_USER_ID + 1,
                "login": constants.GITHUB_ACTIONS_LOGIN,
            }
        ),
        _status(
            creator={"id": constants.GITHUB_ACTIONS_USER_ID, "login": "actions-lookalike[bot]"}
        ),
        _status(target_url="https://evil.example/actions/runs/1"),
        _status(target_url=f"{SERVER_URL}/{REPOSITORY}/actions/runs/{RUN_ID + 1}"),
        _status(description="Codex pending but unstructured"),
        _status(
            state="success",
            description=statuses.boundary_description(_boundary(), state="pending"),
        ),
    ],
)
def test_status_boundary_requires_exact_actor_url_description_and_state(
    status: dict[str, object],
) -> None:
    api = FakeApi(statuses=[status])
    usage = statuses.status_context_usage(api, repository=REPOSITORY, head_sha=HEAD)
    assert (
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
        is None
    )


def test_status_boundary_must_match_the_trusted_run_title() -> None:
    different = _boundary(base="c" * 40)
    api = FakeApi(statuses=[_status()], source_runs={RUN_ID: _source_run(different)})
    usage = statuses.status_context_usage(api, repository=REPOSITORY, head_sha=HEAD)
    assert (
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
        is None
    )


def test_status_boundary_recovers_base_ref_only_from_the_trusted_source() -> None:
    different = _boundary(base_ref="stable")
    api = FakeApi(statuses=[_status()], source_runs={RUN_ID: _source_run(different)})
    usage = statuses.status_context_usage(api, repository=REPOSITORY, head_sha=HEAD)

    assert (
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
        is None
    )


def test_casefold_alias_counts_and_orders_but_cannot_authorize() -> None:
    canonical = _status(status_id=9_001)
    alias = _status(
        status_id=9_002,
        state="success",
        created_at=BOUNDARY_AT + timedelta(minutes=1),
    )
    alias["context"] = "cOdEx rEvIeW"
    api = FakeApi(statuses=[canonical, alias])

    usage = statuses.status_context_usage(api, repository=REPOSITORY, head_sha=HEAD)
    latest = statuses.latest_status_context_page(api, repository=REPOSITORY, head_sha=HEAD)

    assert usage.count == 2
    assert usage.latest is not None and usage.latest.status_id == 9_002
    assert latest is not None and latest.status_id == 9_002
    authoritative = statuses.authoritative_status(
        api,
        usage,
        server_url=SERVER_URL,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        head_sha=HEAD,
        base_sha=BASE,
        base_ref=BASE_REF,
    )
    assert authoritative is not None and authoritative[0].status_id == 9_001
    assert f"/repos/{REPOSITORY}/actions/runs/{RUN_ID}" in api.reads


def test_casefold_alias_alone_never_becomes_authoritative() -> None:
    alias = _status(state="success")
    alias["context"] = "CODEX REVIEW"
    api = FakeApi(statuses=[alias])
    usage = statuses.status_context_usage(api, repository=REPOSITORY, head_sha=HEAD)

    assert usage.count == 1
    assert (
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
        is None
    )
    assert not any("/actions/runs/" in path for path in api.reads)
