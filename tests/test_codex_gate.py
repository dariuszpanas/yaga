"""Integration tests for lifecycle, post-CI review, and finalization operations."""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from tests.codex_support import (
    ACTIONS_USER,
    AUTHOR_ID,
    BASE,
    BOUNDARY_AT,
    CI_WORKFLOW_PATH,
    HEAD,
    LIFECYCLE_WORKFLOW_PATH,
    OUTCOME_AT,
    PULL_REQUEST,
    REPOSITORY,
    RUN_ATTEMPT,
    RUN_ID,
    SERVER_URL,
    FakeApi,
    _association,
    _clean_comment,
    _event,
    _pull_request,
    _reaction,
    _run,
    _status,
)
from yaga.codex.boundary import ReviewBoundary, boundary_description
from yaga.codex.constants import (
    CI_STATUS_CONTEXT,
    CODEX_STATUS_CONTEXT,
    ERROR_WRITE_REQUEST_RESERVE,
    POLL_ITERATION_REQUEST_RESERVE,
)
from yaga.codex.gate import GateResult, authorize, finalize, invalidate, prepare, review
from yaga.codex.runs import SourceRun, load_source_run
from yaga.errors import GateError

BOUNDARY_RUN_ID = RUN_ID - 100
BOUNDARY_RUN_NUMBER = 60
BOUNDARY_URL = f"{SERVER_URL}/{REPOSITORY}/actions/runs/{BOUNDARY_RUN_ID}/attempts/{RUN_ATTEMPT}"
PUBLISHER_URL = f"{SERVER_URL}/{REPOSITORY}/actions/runs/{RUN_ID + 100}/attempts/1"
PROVENANCE_AT = BOUNDARY_AT + timedelta(seconds=10)


def _boundary(action: str = "opened") -> ReviewBoundary:
    return ReviewBoundary(
        BOUNDARY_RUN_ID,
        RUN_ATTEMPT,
        action,
        PULL_REQUEST,
        HEAD,
        BASE,
        BOUNDARY_AT,
    )


def _lifecycle_run(action: str = "opened") -> dict[str, object]:
    return _run(
        run_id=BOUNDARY_RUN_ID,
        run_number=BOUNDARY_RUN_NUMBER,
        path=LIFECYCLE_WORKFLOW_PATH,
        event="pull_request_target",
        display_title=f"YAGA {action} boundary for #{PULL_REQUEST}",
        created_at=BOUNDARY_AT + timedelta(seconds=1),
        updated_at=PROVENANCE_AT,
    )


def _source_run(conclusion: str = "success", action: str = "opened") -> dict[str, object]:
    return _run(
        path=CI_WORKFLOW_PATH,
        event="pull_request",
        conclusion=conclusion,
        display_title=f"YAGA CI {action} for #430 at base {BASE}",
        created_at=BOUNDARY_AT + timedelta(seconds=1),
        updated_at=BOUNDARY_AT + timedelta(minutes=10),
    )


def _workflow_event(source: dict[str, object]) -> dict[str, object]:
    return {
        "action": "completed",
        "repository": {"full_name": REPOSITORY, "default_branch": "main"},
        "workflow_run": copy.deepcopy(source),
    }


def _provenance(action: str = "opened") -> dict[str, object]:
    return _status(
        status_id=8_001,
        context=CODEX_STATUS_CONTEXT,
        state="pending",
        description=boundary_description(_boundary(action), "CI pending"),
        target_url=BOUNDARY_URL,
        created_at=PROVENANCE_AT,
    )


def _codex_pending(*, status_id: int = 8_002, target_url: str = PUBLISHER_URL) -> dict[str, object]:
    return _status(
        status_id=status_id,
        context=CODEX_STATUS_CONTEXT,
        state="pending",
        description=boundary_description(_boundary(), "Codex pending"),
        target_url=target_url,
        created_at=OUTCOME_AT,
        creator=ACTIONS_USER,
    )


def _api(
    *,
    conclusion: str = "success",
    comments: list[dict[str, object]] | None = None,
    reactions: list[dict[str, object]] | None = None,
    statuses: list[dict[str, object]] | None = None,
    pull_request: dict[str, object] | None = None,
    action: str = "opened",
) -> tuple[FakeApi, SourceRun]:
    source = _source_run(conclusion, action)
    lifecycle = _lifecycle_run(action)
    api = FakeApi(
        pull_request=pull_request,
        comments=comments,
        reactions=reactions,
        statuses=statuses or [_provenance(action)],
        current_run=source,
        runs=[source, lifecycle],
    )
    authority = load_source_run(
        api,
        repository=REPOSITORY,
        event_name="workflow_run",
        event=_workflow_event(source),
        workflow=CI_WORKFLOW_PATH,
    )
    return api, authority


def _prepare(api: FakeApi, source: SourceRun, *, direct_id: int = AUTHOR_ID) -> GateResult:
    return prepare(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        wake_workflow=CI_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        direct_author_id=direct_id,
    )


def _authorize_external(api: FakeApi, source: SourceRun) -> GateResult:
    return authorize(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        direct_author_id=AUTHOR_ID + 1,
    )


def test_invalidator_checks_live_candidate_then_writes_both_pending_statuses() -> None:
    api = FakeApi()
    result = invalidate(
        api,
        repository=REPOSITORY,
        event_name="pull_request_target",
        event=_event(),
        run_id=BOUNDARY_RUN_ID,
        run_attempt=1,
        target_url=BOUNDARY_URL,
    )
    assert result.message.startswith("pending:")
    assert api.reads == [
        f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}",
        f"/repos/{REPOSITORY}/commits/{HEAD}/pulls?per_page=100&page=1",
        f"/repos/{REPOSITORY}/commits/{HEAD}/statuses?per_page=100&page=1",
    ]
    assert [payload["context"] for _, payload in api.posts] == [
        CODEX_STATUS_CONTEXT,
        CI_STATUS_CONTEXT,
    ]
    assert all(payload["state"] == "pending" for _, payload in api.posts)

    closed = _pull_request(state="closed")
    closed_api = FakeApi(pull_request=closed)
    assert invalidate(
        closed_api,
        repository=REPOSITORY,
        event_name="pull_request_target",
        event=_event(action="closed", pull_request=closed),
        run_id=BOUNDARY_RUN_ID,
        run_attempt=1,
        target_url=BOUNDARY_URL,
    ).message.startswith("skipped:")
    assert closed_api.posts == []

    delayed_api = FakeApi(pull_request=_pull_request(state="closed"))
    delayed = invalidate(
        delayed_api,
        repository=REPOSITORY,
        event_name="pull_request_target",
        event=_event(),
        run_id=BOUNDARY_RUN_ID,
        run_attempt=1,
        target_url=BOUNDARY_URL,
    )
    assert delayed.message.startswith("skipped:")
    assert delayed_api.posts == []


def test_invalidator_fails_the_native_check_for_an_ambiguous_active_head() -> None:
    api = FakeApi(
        associations=[
            _association(),
            _association(number=PULL_REQUEST + 1),
        ]
    )
    with pytest.raises(GateError, match="not uniquely owned"):
        invalidate(
            api,
            repository=REPOSITORY,
            event_name="pull_request_target",
            event=_event(),
            run_id=BOUNDARY_RUN_ID,
            run_attempt=1,
            target_url=BOUNDARY_URL,
        )
    assert api.posts == []


def test_prepare_routes_owner_external_and_existing_automatic_review() -> None:
    owner_api, source = _api()
    assert _prepare(owner_api, source).route == "owner"
    assert all("comments" not in path for path, _ in owner_api.posts)

    external_api, source = _api()
    assert _prepare(external_api, source, direct_id=AUTHOR_ID + 1).route == "external"

    observing_api, source = _api(reactions=[_reaction(content="eyes")])
    assert _prepare(observing_api, source).route == "observe"


def test_protected_approval_is_a_separate_non_triggering_route_capability() -> None:
    api, source = _api()
    assert _authorize_external(api, source).route == "done"
    assert _prepare(api, source, direct_id=AUTHOR_ID + 1).route == "approved"
    comments = [payload for path, payload in api.posts if path.endswith("/comments")]
    assert len(comments) == 1
    assert str(comments[0]["body"]).startswith("YAGA recorded maintainer approval")
    assert "@codex" not in str(comments[0]["body"])

    owner_api, owner_source = _api()
    with pytest.raises(GateError, match="owner-authored"):
        authorize(
            owner_api,
            repository=REPOSITORY,
            source=owner_source,
            lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
            server_url=SERVER_URL,
            direct_author_id=AUTHOR_ID,
        )


def test_prepare_waits_for_the_concurrent_lifecycle_invalidator() -> None:
    source_payload = _source_run()
    lifecycle = _lifecycle_run()
    api = FakeApi(statuses=[], current_run=source_payload, runs=[source_payload, lifecycle])
    source = load_source_run(
        api,
        repository=REPOSITORY,
        event_name="workflow_run",
        event=_workflow_event(source_payload),
        workflow=CI_WORKFLOW_PATH,
    )
    now = [0.0]
    sleeps: list[float] = []

    def finish_lifecycle(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds
        api.statuses.append(_provenance())

    result = prepare(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        wake_workflow=CI_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        direct_author_id=AUTHOR_ID,
        lifecycle_deadline=60.0,
        clock=lambda: now[0],
        sleep=finish_lifecycle,
    )

    assert result.route == "owner"
    assert sleeps == [30.0]


def test_prepare_elects_exactly_one_completion_wake_before_any_write() -> None:
    lifecycle_later_api, source = _api()
    lifecycle_later = BOUNDARY_AT + timedelta(minutes=20)
    for run in lifecycle_later_api.runs:
        if run["path"] == LIFECYCLE_WORKFLOW_PATH:
            run["updated_at"] = lifecycle_later.isoformat(timespec="seconds").replace("+00:00", "Z")

    skipped = _prepare(lifecycle_later_api, source)
    assert skipped.route == "skip"
    assert skipped.message == "skipped: another completion wake owns this boundary"
    assert lifecycle_later_api.posts == []

    elected = prepare(
        lifecycle_later_api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        wake_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        direct_author_id=AUTHOR_ID,
    )
    assert elected.route == "owner"

    tied_api, tied_source = _api()
    for run in tied_api.runs:
        if run["path"] == LIFECYCLE_WORKFLOW_PATH:
            run["updated_at"] = tied_source.updated_at.isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
    assert _prepare(tied_api, tied_source).route == "owner"

    tied_lifecycle_api, tied_lifecycle_source = _api()
    for run in tied_lifecycle_api.runs:
        if run["path"] == LIFECYCLE_WORKFLOW_PATH:
            run["updated_at"] = tied_lifecycle_source.updated_at.isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
    tied_lifecycle = prepare(
        tied_lifecycle_api,
        repository=REPOSITORY,
        source=tied_lifecycle_source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        wake_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        direct_author_id=AUTHOR_ID,
    )
    assert tied_lifecycle.route == "skip"
    assert tied_lifecycle_api.posts == []


def test_metadata_only_edit_run_does_not_supersede_current_boundary() -> None:
    api, source = _api()
    api.runs.insert(
        0,
        _run(
            run_id=BOUNDARY_RUN_ID + 1,
            run_number=BOUNDARY_RUN_NUMBER + 1,
            path=LIFECYCLE_WORKFLOW_PATH,
            event="pull_request_target",
            display_title=f"YAGA metadata edit for #{PULL_REQUEST}",
            created_at=BOUNDARY_AT + timedelta(minutes=1),
        ),
    )
    assert _prepare(api, source).route == "owner"


def test_newer_real_boundary_run_supersedes_even_when_it_wrote_no_status() -> None:
    api, source = _api()
    api.runs.insert(
        0,
        _run(
            run_id=BOUNDARY_RUN_ID + 1,
            run_number=BOUNDARY_RUN_NUMBER + 1,
            path=LIFECYCLE_WORKFLOW_PATH,
            event="pull_request_target",
            status="completed",
            conclusion="failure",
            display_title=f"YAGA reopened boundary for #{PULL_REQUEST}",
            created_at=BOUNDARY_AT + timedelta(minutes=1),
        ),
    )
    with pytest.raises(GateError, match="newer lifecycle boundary"):
        _prepare(api, source)


def test_old_ci_action_cannot_authorize_a_new_ready_boundary() -> None:
    source_payload = _source_run()
    lifecycle = _lifecycle_run("ready_for_review")
    boundary = _boundary("ready_for_review")
    api = FakeApi(
        statuses=[
            _status(
                status_id=8_001,
                context=CODEX_STATUS_CONTEXT,
                state="pending",
                description=boundary_description(boundary, "CI pending"),
                target_url=BOUNDARY_URL,
                created_at=PROVENANCE_AT,
            )
        ],
        current_run=source_payload,
        runs=[source_payload, lifecycle],
    )
    source = load_source_run(
        api,
        repository=REPOSITORY,
        event_name="workflow_run",
        event=_workflow_event(source_payload),
        workflow=CI_WORKFLOW_PATH,
    )
    with pytest.raises(GateError, match="current lifecycle boundary"):
        _prepare(api, source)


def test_source_ci_base_binding_rejects_a_later_base_edit_and_rerun() -> None:
    old_base = "c" * 40
    source_payload = _source_run()
    source_payload["display_title"] = f"YAGA CI opened for #430 at base {old_base}"
    lifecycle = _lifecycle_run("edited")
    boundary = _boundary("edited")
    api = FakeApi(
        statuses=[
            _status(
                status_id=8_001,
                context=CODEX_STATUS_CONTEXT,
                state="pending",
                description=boundary_description(boundary, "CI pending"),
                target_url=BOUNDARY_URL,
                created_at=PROVENANCE_AT,
            )
        ],
        current_run=source_payload,
        runs=[source_payload, lifecycle],
    )
    source = load_source_run(
        api,
        repository=REPOSITORY,
        event_name="workflow_run",
        event=_workflow_event(source_payload),
        workflow=CI_WORKFLOW_PATH,
    )
    assert _prepare(api, source).message.startswith("skipped:")


def test_prepare_accepts_clean_first_review_reaction_without_comment() -> None:
    api, source = _api(reactions=[_reaction(content="+1")])
    result = _prepare(api, source)
    assert result == GateResult("success: Codex already reviewed the exact head", 0, "done")
    assert [payload["state"] for _, payload in api.posts] == ["pending", "pending", "success"]
    assert all("comments" not in path for path, _ in api.posts)


def test_ready_boundary_does_not_bind_an_unrequested_clean_reaction() -> None:
    api, source = _api(action="ready_for_review", reactions=[_reaction(content="+1")])
    assert _prepare(api, source).route == "owner"


@pytest.mark.parametrize(("delay_seconds", "expected_exit"), [(0, 1), (1, 0)])
def test_later_boundary_reaction_must_follow_the_exact_request_by_a_full_second(
    delay_seconds: int,
    expected_exit: int,
) -> None:
    class CompletingApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            value = super().post(path, payload)
            if path.endswith("/comments"):
                self.reactions.append(
                    _reaction(
                        reaction_id=55_002,
                        created_at=BOUNDARY_AT + timedelta(hours=2, seconds=delay_seconds),
                    )
                )
            return value

    base_api, source = _api(action="synchronize")
    api = CompletingApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
    )
    result = review(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        allow_request=True,
        direct_author_id=AUTHOR_ID,
        poll_deadline=0.0,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    assert result.exit_code == expected_exit
    request_posts = [payload for path, payload in api.posts if path.endswith("/comments")]
    assert len(request_posts) == 1


def test_external_author_cannot_bypass_environment_with_unsolicited_codex_outcome() -> None:
    api, source = _api(reactions=[_reaction(content="+1")])
    result = _prepare(api, source, direct_id=AUTHOR_ID + 1)
    assert result.route == "external"
    assert [payload["state"] for _, payload in api.posts] == ["pending", "pending"]


def test_failed_ci_never_requests_codex_and_fails_both_authoritative_statuses() -> None:
    api, source = _api(conclusion="failure")
    result = _prepare(api, source)
    assert result.exit_code == 1
    assert [payload["context"] for _, payload in api.posts] == [
        CODEX_STATUS_CONTEXT,
        CODEX_STATUS_CONTEXT,
        CI_STATUS_CONTEXT,
        CI_STATUS_CONTEXT,
    ]
    assert [payload["state"] for _, payload in api.posts] == [
        "pending",
        "error",
        "pending",
        "error",
    ]
    assert all("comments" not in path for path, _ in api.posts)


def test_owner_request_posts_once_then_publishes_codex_success() -> None:
    class CompletingApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            value = super().post(path, payload)
            if path.endswith("/comments"):
                self.comments.append(_clean_comment(comment_id=55_001, created_at=OUTCOME_AT))
            return value

    base_api, source = _api()
    api = CompletingApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
    )
    result = review(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        allow_request=True,
        direct_author_id=AUTHOR_ID,
        poll_deadline=100.0,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
    assert result.route == "done"
    requests = [payload for path, payload in api.posts if path.endswith("/comments")]
    assert len(requests) == 1
    assert str(requests[0]["body"]).startswith("@codex review\n\n")
    assert [
        payload["state"]
        for path, payload in api.posts
        if "/statuses/" in path and payload["context"] == CODEX_STATUS_CONTEXT
    ] == ["pending", "success"]


def test_protected_external_authorization_is_non_triggering_and_reuses_existing_review() -> None:
    api, source = _api(reactions=[_reaction(content="+1")])
    assert _authorize_external(api, source).route == "done"
    result = review(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        allow_request=True,
        direct_author_id=AUTHOR_ID + 1,
        poll_deadline=100.0,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
    assert result.route == "done"
    comments = [payload for path, payload in api.posts if path.endswith("/comments")]
    assert len(comments) == 1
    assert str(comments[0]["body"]).startswith("YAGA recorded maintainer approval")
    assert "@codex" not in str(comments[0]["body"])


def test_request_revalidates_close_before_comment_and_does_no_postclose_write() -> None:
    class ClosingApi(FakeApi):
        status_writes = 0

        def post(self, path: str, payload: dict[str, object]) -> object:
            value = super().post(path, payload)
            if "/statuses/" in path:
                self.status_writes += 1
                if self.status_writes == 1:
                    self.pull_requests[PULL_REQUEST]["state"] = "closed"
                    self.associations[0]["state"] = "closed"
            return value

    base_api, source = _api()
    api = ClosingApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
    )
    result = review(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        allow_request=True,
        direct_author_id=AUTHOR_ID,
        poll_deadline=0.0,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
    assert result.message.startswith("skipped:")
    assert len(api.posts) == 1
    assert all(not path.endswith("/comments") for path, _ in api.posts)


def test_finalize_rechecks_evidence_before_publishing_ci_gate_success() -> None:
    passed = _status(
        status_id=8_003,
        context=CODEX_STATUS_CONTEXT,
        state="success",
        description=boundary_description(_boundary(), "Codex passed"),
        target_url=PUBLISHER_URL,
        created_at=OUTCOME_AT + timedelta(minutes=1),
        creator=ACTIONS_USER,
    )
    api, source = _api(
        reactions=[_reaction(content="+1")],
        statuses=[_provenance(), _codex_pending(), passed],
    )
    result = finalize(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        direct_author_id=AUTHOR_ID,
    )
    assert result.route == "done"
    assert [payload["context"] for _, payload in api.posts] == [
        CI_STATUS_CONTEXT,
        CI_STATUS_CONTEXT,
    ]
    assert [payload["state"] for _, payload in api.posts] == ["pending", "success"]


def test_codex_success_is_repaired_to_pending_when_exact_outcome_disappears() -> None:
    class OutcomeRaceApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            value = super().post(path, payload)
            if (
                path.endswith(f"/statuses/{HEAD}")
                and payload.get("context") == CODEX_STATUS_CONTEXT
                and payload.get("state") == "success"
            ):
                self.reactions.clear()
            return value

    base_api, source = _api(reactions=[_reaction(content="+1")])
    api = OutcomeRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        reactions=base_api.reactions,
    )

    result = _prepare(api, source)

    assert result.message.startswith("skipped:")
    codex_writes = [
        payload
        for path, payload in api.posts
        if "/statuses/" in path and payload["context"] == CODEX_STATUS_CONTEXT
    ]
    assert [payload["state"] for payload in codex_writes] == [
        "pending",
        "success",
        "pending",
    ]


def test_codex_success_is_repaired_to_pending_when_candidate_closes() -> None:
    class CloseRaceApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            value = super().post(path, payload)
            if (
                path.endswith(f"/statuses/{HEAD}")
                and payload.get("context") == CODEX_STATUS_CONTEXT
                and payload.get("state") == "success"
            ):
                self.pull_requests[PULL_REQUEST]["state"] = "closed"
                self.associations[0]["state"] = "closed"
            return value

    base_api, source = _api(reactions=[_reaction(content="+1")])
    api = CloseRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        reactions=base_api.reactions,
    )

    result = _prepare(api, source)

    assert result.message.startswith("skipped:")
    codex_writes = [
        payload for _, payload in api.posts if payload["context"] == CODEX_STATUS_CONTEXT
    ]
    assert [payload["state"] for payload in codex_writes] == [
        "pending",
        "success",
        "pending",
    ]


def test_final_ci_success_is_repaired_when_codex_evidence_disappears() -> None:
    passed = _status(
        status_id=8_003,
        context=CODEX_STATUS_CONTEXT,
        state="success",
        description=boundary_description(_boundary(), "Codex passed"),
        target_url=PUBLISHER_URL,
        created_at=OUTCOME_AT + timedelta(minutes=1),
        creator=ACTIONS_USER,
    )

    class FinalOutcomeRaceApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            value = super().post(path, payload)
            if (
                path.endswith(f"/statuses/{HEAD}")
                and payload.get("context") == CI_STATUS_CONTEXT
                and payload.get("state") == "success"
            ):
                self.reactions.clear()
            return value

    base_api, source = _api(
        reactions=[_reaction(content="+1")],
        statuses=[_provenance(), _codex_pending(), passed],
    )
    api = FinalOutcomeRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        reactions=base_api.reactions,
    )

    result = finalize(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        direct_author_id=AUTHOR_ID,
    )

    assert result.message.startswith("skipped:")
    ci_writes = [payload for _, payload in api.posts if payload["context"] == CI_STATUS_CONTEXT]
    assert [payload["state"] for payload in ci_writes] == [
        "pending",
        "success",
        "pending",
    ]


def test_external_success_is_repaired_when_approval_marker_is_edited() -> None:
    class AuthorizationRaceApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            value = super().post(path, payload)
            if (
                path.endswith(f"/statuses/{HEAD}")
                and payload.get("context") == CODEX_STATUS_CONTEXT
                and payload.get("state") == "success"
            ):
                for comment in self.comments:
                    if str(comment.get("body", "")).startswith("YAGA recorded maintainer"):
                        comment["body"] = "edited after approval"
            return value

    base_api, source = _api(reactions=[_reaction(content="+1")])
    assert _authorize_external(base_api, source).route == "done"
    api = AuthorizationRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        reactions=base_api.reactions,
        comments=base_api.comments,
    )

    result = review(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        allow_request=True,
        direct_author_id=AUTHOR_ID + 1,
        poll_deadline=100.0,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    assert result.message.startswith("skipped:")
    codex_writes = [
        payload
        for path, payload in api.posts
        if "/statuses/" in path and payload["context"] == CODEX_STATUS_CONTEXT
    ]
    assert [payload["state"] for payload in codex_writes] == [
        "pending",
        "success",
        "pending",
    ]


def test_poll_budget_cutoff_preserves_the_terminal_error_tail() -> None:
    class NearBudgetApi(FakeApi):
        def require_success_tail(self, count: int, *, cleanup_margin_seconds: int) -> None:
            super().require_success_tail(count, cleanup_margin_seconds=cleanup_margin_seconds)
            if count == POLL_ITERATION_REQUEST_RESERVE:
                raise GateError("poll iteration no longer fits")

    base_api, source = _api()
    api = NearBudgetApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
    )

    result = review(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        allow_request=True,
        direct_author_id=AUTHOR_ID,
        poll_deadline=100.0,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    assert result.exit_code == 1
    assert api.success_tail_calls == [
        (POLL_ITERATION_REQUEST_RESERVE, 60),
        (ERROR_WRITE_REQUEST_RESERVE, 60),
    ]
    codex_writes = [
        payload
        for path, payload in api.posts
        if "/statuses/" in path and payload["context"] == CODEX_STATUS_CONTEXT
    ]
    assert [payload["state"] for payload in codex_writes] == ["pending", "error"]


def test_finalize_rejects_a_success_that_leapfrogged_a_newer_pending() -> None:
    owner_target = PUBLISHER_URL
    newer_target = PUBLISHER_URL.replace("/attempts/1", "/attempts/2")
    passed = _status(
        status_id=8_004,
        context=CODEX_STATUS_CONTEXT,
        state="success",
        description=boundary_description(_boundary(), "Codex passed"),
        target_url=owner_target,
        created_at=OUTCOME_AT + timedelta(seconds=2),
        creator=ACTIONS_USER,
    )
    api, source = _api(
        reactions=[_reaction(content="+1")],
        statuses=[
            _provenance(),
            _codex_pending(status_id=8_002, target_url=owner_target),
            _status(
                status_id=8_003,
                context=CODEX_STATUS_CONTEXT,
                state="pending",
                description=boundary_description(_boundary(), "Codex pending"),
                target_url=newer_target,
                created_at=OUTCOME_AT + timedelta(seconds=1),
                creator=ACTIONS_USER,
            ),
            passed,
        ],
    )

    result = finalize(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        direct_author_id=AUTHOR_ID,
    )

    assert result.exit_code == 1
    ci_writes = [payload for _, payload in api.posts if payload["context"] == CI_STATUS_CONTEXT]
    assert [payload["state"] for payload in ci_writes] == ["pending", "error"]
