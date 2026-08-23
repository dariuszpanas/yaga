"""Tests for live Codex candidate and ownership discovery."""

from __future__ import annotations

import pytest

from tests.codex_support import (
    BASE,
    BASE_REF,
    HEAD,
    HEAD_REF,
    HEAD_REPOSITORY,
    PULL_REQUEST,
    REPOSITORY,
    RUN_ID,
    FakeApi,
    _association,
    _pull_request,
    _review_observer_run,
    _workflow_run_event,
)
from yaga.codex import candidates
from yaga.codex.models import Candidate
from yaga.errors import GateError


def test_connector_review_observer_resolves_the_unique_live_head_owner() -> None:
    source = _review_observer_run()
    api = FakeApi(source_runs={RUN_ID: source})

    assert candidates.connector_review_workflow_candidate(
        api,
        repository=REPOSITORY,
        event=_workflow_run_event(source),
    ) == Candidate(
        pull_request_number=PULL_REQUEST,
        head_sha=HEAD,
        base_sha=BASE,
        base_ref=BASE_REF,
        head_repository=HEAD_REPOSITORY,
        head_ref=HEAD_REF,
    )


@pytest.mark.parametrize(
    "source",
    [
        _review_observer_run(actor={"id": 50_001, "login": "repository-owner"}),
        _review_observer_run(path=".github/workflows/lookalike.yml"),
        {**_review_observer_run(), "conclusion": "cancelled"},
    ],
)
def test_connector_review_observer_rejects_untrusted_source(
    source: dict[str, object],
) -> None:
    api = FakeApi(source_runs={RUN_ID: source})
    with pytest.raises(GateError):
        candidates.connector_review_workflow_candidate(
            api,
            repository=REPOSITORY,
            event=_workflow_run_event(source),
        )


def test_connector_review_observer_returns_none_after_the_head_closes() -> None:
    source = _review_observer_run()
    api = FakeApi(
        pull_request=_pull_request(state="closed"),
        associations=[_association(state="closed")],
        source_runs={RUN_ID: source},
    )

    assert (
        candidates.connector_review_workflow_candidate(
            api,
            repository=REPOSITORY,
            event=_workflow_run_event(source),
        )
        is None
    )
