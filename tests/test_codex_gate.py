"""Integration tests for lifecycle, post-CI review, and finalization operations."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

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
    _review,
    _run,
    _status,
)
from yaga.agent_review.github.boundary import ReviewBoundary, boundary_description
from yaga.agent_review.github.constants import (
    CI_STATUS_CONTEXT,
    CODEX_STATUS_CONTEXT,
    ERROR_WRITE_REQUEST_RESERVE,
    POLL_ITERATION_REQUEST_RESERVE,
)
from yaga.agent_review.github.gate import (
    GateResult,
    authorize,
    finalize,
    invalidate,
    prepare,
    review,
)
from yaga.agent_review.github.requests import RequestKey
from yaga.agent_review.github.runs import SourceRun, load_source_run
from yaga.errors import GateError

BOUNDARY_RUN_ID = RUN_ID - 100
BOUNDARY_RUN_NUMBER = 60
BOUNDARY_URL = f"{SERVER_URL}/{REPOSITORY}/actions/runs/{BOUNDARY_RUN_ID}/attempts/{RUN_ATTEMPT}"
PUBLISHER_URL = f"{SERVER_URL}/{REPOSITORY}/actions/runs/{RUN_ID + 100}/attempts/1"
PROVENANCE_AT = BOUNDARY_AT + timedelta(seconds=10)
REQUEST_AT = BOUNDARY_AT + timedelta(hours=2)
POST_REQUEST_AT = REQUEST_AT + timedelta(seconds=1)


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
    reviews: list[dict[str, object]] | None = None,
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
        reviews=reviews,
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


def _request_key(
    *,
    head_sha: str = HEAD,
    base_sha: str = BASE,
    boundary_run_id: int = BOUNDARY_RUN_ID,
) -> RequestKey:
    return RequestKey(
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        head_sha=head_sha,
        base_sha=base_sha,
        boundary_run_id=boundary_run_id,
        source_run_id=RUN_ID,
        source_run_attempt=RUN_ATTEMPT,
    )


def _add_current_request(
    api: FakeApi,
    *,
    created_at: datetime = REQUEST_AT,
    comment_id: int = 30_001,
) -> dict[str, object]:
    timestamp = created_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    comment: dict[str, object] = {
        "id": comment_id,
        "body": _request_key().body,
        "user": copy.deepcopy(ACTIONS_USER),
        "performed_via_github_app": {"id": 15_368, "slug": "github-actions"},
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    api.comments.append(comment)
    return comment


def _set_comment_timestamp(comment: dict[str, object], created_at: datetime) -> None:
    timestamp = created_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    comment["created_at"] = timestamp
    comment["updated_at"] = timestamp


def _mutate_current_request(api: FakeApi, mutation: str) -> None:
    if mutation == "delete":
        api.comments[:] = [
            comment
            for comment in api.comments
            if not str(comment.get("body", "")).startswith("@codex review")
        ]
        return
    if mutation == "edit":
        for comment in api.comments:
            if str(comment.get("body", "")).startswith("@codex review"):
                comment["body"] = "edited after request"
                return
    raise AssertionError(f"could not {mutation} the current request")


def _publish_unsettled_prior_request(api: FakeApi) -> None:
    prior = _request_key(
        base_sha="c" * 40,
        boundary_run_id=BOUNDARY_RUN_ID - 1,
    )
    api.post(
        f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/comments",
        {"body": prior.body},
    )
    created_at = (BOUNDARY_AT - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    api.comments[-1]["created_at"] = created_at
    api.comments[-1]["updated_at"] = created_at


def _add_unsettled_prior_request(api: FakeApi) -> None:
    _publish_unsettled_prior_request(api)
    api.posts.clear()


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


def test_prepare_routes_owner_external_and_exact_request_observe() -> None:
    owner_api, source = _api()
    assert _prepare(owner_api, source).route == "owner"
    assert all("comments" not in path for path, _ in owner_api.posts)

    external_api, source = _api()
    assert _prepare(external_api, source, direct_id=AUTHOR_ID + 1).route == "external"

    observing_api, source = _api(reactions=[_reaction(content="eyes", created_at=POST_REQUEST_AT)])
    _add_current_request(observing_api)
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


@pytest.mark.parametrize(
    "activity",
    ["eyes", "+1", "completion comment", "formal review"],
)
def test_opened_unsolicited_connector_activity_fails_without_a_request(
    activity: str,
) -> None:
    comments = [_clean_comment()] if activity == "completion comment" else None
    reviews = [_review()] if activity == "formal review" else None
    reactions = [_reaction(content=activity)] if activity in {"eyes", "+1"} else None
    api, source = _api(comments=comments, reviews=reviews, reactions=reactions)

    result = _prepare(api, source)

    assert result.exit_code == 1
    assert result.route == "skip"
    assert "no exact YAGA request" in result.message
    assert all(not path.endswith("/comments") for path, _ in api.posts)
    assert all(payload.get("state") != "success" for _, payload in api.posts)


@pytest.mark.parametrize("outcome_kind", ["+1", "completion comment", "formal review"])
def test_prepare_accepts_every_outcome_strictly_after_the_exact_request(
    outcome_kind: str,
) -> None:
    comments = (
        [_clean_comment(created_at=POST_REQUEST_AT)]
        if outcome_kind == "completion comment"
        else None
    )
    reviews = [_review(submitted_at=POST_REQUEST_AT)] if outcome_kind == "formal review" else None
    reactions = (
        [_reaction(content="+1", created_at=POST_REQUEST_AT)] if outcome_kind == "+1" else None
    )
    api, source = _api(comments=comments, reviews=reviews, reactions=reactions)
    _add_current_request(api)

    result = _prepare(api, source)

    assert result == GateResult("success: Codex already reviewed the exact head", 0, "done")
    codex_writes = [
        payload
        for path, payload in api.posts
        if "/statuses/" in path and payload["context"] == CODEX_STATUS_CONTEXT
    ]
    assert [payload["state"] for payload in codex_writes] == ["pending", "success"]


@pytest.mark.parametrize("outcome_kind", ["+1", "completion comment", "formal review"])
def test_same_second_outcome_cannot_follow_the_exact_request(outcome_kind: str) -> None:
    comments = (
        [_clean_comment(created_at=REQUEST_AT)] if outcome_kind == "completion comment" else None
    )
    reviews = [_review(submitted_at=REQUEST_AT)] if outcome_kind == "formal review" else None
    reactions = [_reaction(content="+1", created_at=REQUEST_AT)] if outcome_kind == "+1" else None
    api, source = _api(comments=comments, reviews=reviews, reactions=reactions)
    _add_current_request(api)

    result = _prepare(api, source)

    assert result.route == "observe"
    assert all(payload.get("state") != "success" for _, payload in api.posts)


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


def test_unsettled_prior_yaga_request_blocks_a_new_boundary_request() -> None:
    api, source = _api(action="synchronize")
    api.post(
        f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/comments",
        {"body": _request_key(boundary_run_id=BOUNDARY_RUN_ID - 1).body},
    )
    api.posts.clear()

    result = _prepare(api, source)

    assert result.exit_code == 1
    assert "earlier YAGA request" in result.message
    assert all(not path.endswith("/comments") for path, _ in api.posts)
    assert [payload["state"] for _, payload in api.posts] == [
        "pending",
        "pending",
        "error",
        "pending",
        "error",
    ]


@pytest.mark.parametrize("outcome_kind", ["completion comment", "formal review"])
def test_unsettled_prior_request_blocks_delayed_same_head_outcome(
    outcome_kind: str,
) -> None:
    comments = (
        [_clean_comment(created_at=POST_REQUEST_AT)]
        if outcome_kind == "completion comment"
        else None
    )
    reviews = [_review(submitted_at=POST_REQUEST_AT)] if outcome_kind == "formal review" else None
    api, source = _api(
        action="ready_for_review",
        comments=comments,
        reviews=reviews,
    )
    _add_unsettled_prior_request(api)
    _add_current_request(api)

    result = _prepare(api, source)

    assert result.exit_code == 1
    assert "earlier YAGA request" in result.message
    assert all(payload["state"] != "success" for _, payload in api.posts)


def test_codex_settlement_rechecks_prior_history_after_outcome_selection() -> None:
    class PriorRequestRaceApi(FakeApi):
        injected = False

        def get(self, path: str) -> object:
            if path.endswith("/reviews/6000") and not self.injected:
                self.injected = True
                _publish_unsettled_prior_request(self)
            return super().get(path)

    base_api, source = _api(
        action="ready_for_review",
        reviews=[_review(submitted_at=POST_REQUEST_AT)],
    )
    api = PriorRequestRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        reviews=base_api.reviews,
    )
    _add_current_request(api)

    result = _prepare(api, source)

    assert api.injected
    assert result.message.startswith("skipped:")
    codex_writes = [
        payload
        for path, payload in api.posts
        if "/statuses/" in path and payload["context"] == CODEX_STATUS_CONTEXT
    ]
    assert [payload["state"] for payload in codex_writes] == ["pending"]


@pytest.mark.parametrize(
    ("prior_context", "expected_exit"),
    [(CODEX_STATUS_CONTEXT, 0), ("codex review", 1)],
)
def test_only_exact_context_prior_success_allows_a_new_boundary_request(
    prior_context: str,
    expected_exit: int,
) -> None:
    prior_head = "c" * 40
    prior_run_id = BOUNDARY_RUN_ID - 1
    prior_boundary = ReviewBoundary(
        prior_run_id,
        1,
        "opened",
        PULL_REQUEST,
        prior_head,
        BASE,
        BOUNDARY_AT - timedelta(hours=1),
    )
    prior_pending = _status(
        status_id=7_901,
        context=prior_context,
        state="pending",
        description=boundary_description(prior_boundary, "Codex pending"),
        target_url=PUBLISHER_URL,
        created_at=BOUNDARY_AT + timedelta(hours=2, seconds=1),
    )
    prior_success = _status(
        status_id=7_902,
        context=prior_context,
        state="success",
        description=boundary_description(prior_boundary, "Codex passed"),
        target_url=PUBLISHER_URL,
        created_at=BOUNDARY_AT + timedelta(hours=3),
    )
    base_api, source = _api(action="synchronize")
    api = FakeApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        statuses_by_head={prior_head: [prior_pending, prior_success]},
    )
    api.post(
        f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/comments",
        {"body": _request_key(head_sha=prior_head, boundary_run_id=prior_run_id).body},
    )
    api.posts.clear()

    result = _prepare(api, source)
    assert result.exit_code == expected_exit
    assert result.route == ("owner" if expected_exit == 0 else "skip")
    assert all(not path.endswith("/comments") for path, _ in api.posts)


def test_external_author_cannot_bypass_environment_with_unsolicited_codex_outcome() -> None:
    api, source = _api(reactions=[_reaction(content="+1")])
    result = _prepare(api, source, direct_id=AUTHOR_ID + 1)
    assert result.exit_code == 1
    assert result.route == "skip"
    assert "no exact YAGA request" in result.message
    assert all(not path.endswith("/comments") for path, _ in api.posts)


def test_observe_worker_fails_closed_without_the_exact_request() -> None:
    api, source = _api()

    result = review(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        allow_request=False,
        direct_author_id=AUTHOR_ID,
        poll_deadline=0.0,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    assert result.exit_code == 1
    assert "exact YAGA request is missing" in result.message
    assert all(not path.endswith("/comments") for path, _ in api.posts)


def test_external_exact_request_without_protected_approval_fails_closed() -> None:
    api, source = _api(comments=[_clean_comment(created_at=POST_REQUEST_AT)])
    _add_current_request(api)

    prepared = _prepare(api, source, direct_id=AUTHOR_ID + 1)

    assert prepared.exit_code == 1
    assert "lacks protected approval" in prepared.message
    assert all(payload.get("state") != "success" for _, payload in api.posts)

    worker_api, worker_source = _api(comments=[_clean_comment(created_at=POST_REQUEST_AT)])
    _add_current_request(worker_api)
    with pytest.raises(GateError, match="lacks protected approval"):
        review(
            worker_api,
            repository=REPOSITORY,
            source=worker_source,
            lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
            server_url=SERVER_URL,
            target_url=PUBLISHER_URL,
            allow_request=False,
            direct_author_id=AUTHOR_ID + 1,
            poll_deadline=0.0,
            clock=lambda: 0.0,
            sleep=lambda _seconds: None,
        )


@pytest.mark.parametrize("approval_offset", [0, 1])
def test_external_approval_cannot_retroactively_authorize_a_request(
    approval_offset: int,
) -> None:
    api, source = _api(
        comments=[
            _clean_comment(created_at=REQUEST_AT + timedelta(seconds=2)),
        ]
    )
    _add_current_request(api)
    assert _authorize_external(api, source).route == "done"
    approval = next(
        comment
        for comment in api.comments
        if str(comment.get("body", "")).startswith("YAGA recorded maintainer approval")
    )
    _set_comment_timestamp(
        approval,
        REQUEST_AT + timedelta(seconds=approval_offset),
    )
    api.posts.clear()

    result = _prepare(api, source, direct_id=AUTHOR_ID + 1)

    assert result.exit_code == 1
    assert "does not follow protected approval" in result.message
    assert all(payload.get("state") != "success" for _, payload in api.posts)


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
                self.comments.append(_clean_comment(comment_id=55_001, created_at=POST_REQUEST_AT))
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


def test_owner_request_suppresses_unsolicited_activity_immediately_before_post() -> None:
    class CompletingBeforePostApi(FakeApi):
        reaction_reads = 0

        def paginate(self, path: str) -> list[dict[str, object]]:
            if path.endswith("/reactions"):
                self.reaction_reads += 1
                if self.reaction_reads == 3:
                    self.reactions.append(_reaction(content="+1"))
            return super().paginate(path)

    base_api, source = _api()
    api = CompletingBeforePostApi(
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
    assert "activity appeared before its request" in result.message
    assert all(not path.endswith("/comments") for path, _ in api.posts)
    assert [
        payload["state"]
        for path, payload in api.posts
        if "/statuses/" in path and payload["context"] == CODEX_STATUS_CONTEXT
    ] == ["pending", "error"]


def test_protected_external_authorization_never_reuses_unsolicited_review() -> None:
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
    assert result.exit_code == 1
    assert "no exact YAGA request" in result.message
    comments = [payload for path, payload in api.posts if path.endswith("/comments")]
    assert len(comments) == 1
    assert str(comments[0]["body"]).startswith("YAGA recorded maintainer approval")
    assert "@codex" not in str(comments[0]["body"])


def test_external_request_rechecks_protected_approval_immediately_before_post() -> None:
    class DeletingApprovalApi(FakeApi):
        comment_reads = 0

        def paginate(self, path: str) -> list[dict[str, object]]:
            if path.endswith("/comments"):
                self.comment_reads += 1
                if self.comment_reads == 5:
                    self.comments.clear()
            return super().paginate(path)

    base_api, source = _api()
    assert _authorize_external(base_api, source).route == "done"
    api = DeletingApprovalApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        comments=base_api.comments,
    )

    with pytest.raises(GateError, match="immediately before publication"):
        review(
            api,
            repository=REPOSITORY,
            source=source,
            lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
            server_url=SERVER_URL,
            target_url=PUBLISHER_URL,
            allow_request=True,
            direct_author_id=AUTHOR_ID + 1,
            poll_deadline=0.0,
            clock=lambda: 0.0,
            sleep=lambda _seconds: None,
        )

    assert all(
        not (
            path.endswith("/comments") and str(payload.get("body", "")).startswith("@codex review")
        )
        for path, payload in api.posts
    )


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
        created_at=POST_REQUEST_AT,
        creator=ACTIONS_USER,
    )
    api, source = _api(
        reactions=[_reaction(content="+1", created_at=POST_REQUEST_AT)],
        statuses=[_provenance(), _codex_pending(), passed],
    )
    _add_current_request(api)
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


def test_finalize_rejects_unsolicited_outcome_without_posting_a_request() -> None:
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
        comments=[_clean_comment()],
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

    assert result.exit_code == 1
    assert all(not path.endswith("/comments") for path, _ in api.posts)
    ci_writes = [payload for _, payload in api.posts if payload["context"] == CI_STATUS_CONTEXT]
    assert [payload["state"] for payload in ci_writes] == ["pending", "error"]


@pytest.mark.parametrize("outcome_kind", ["completion comment", "formal review"])
def test_finalize_rejects_delayed_same_head_outcome_with_unsettled_prior_request(
    outcome_kind: str,
) -> None:
    boundary = _boundary("ready_for_review")
    pending = _status(
        status_id=8_003,
        context=CODEX_STATUS_CONTEXT,
        state="pending",
        description=boundary_description(boundary, "Codex pending"),
        target_url=PUBLISHER_URL,
        created_at=OUTCOME_AT,
        creator=ACTIONS_USER,
    )
    passed = _status(
        status_id=8_004,
        context=CODEX_STATUS_CONTEXT,
        state="success",
        description=boundary_description(boundary, "Codex passed"),
        target_url=PUBLISHER_URL,
        created_at=OUTCOME_AT + timedelta(minutes=1),
        creator=ACTIONS_USER,
    )
    comments = (
        [_clean_comment(created_at=POST_REQUEST_AT)]
        if outcome_kind == "completion comment"
        else None
    )
    reviews = [_review(submitted_at=POST_REQUEST_AT)] if outcome_kind == "formal review" else None
    api, source = _api(
        action="ready_for_review",
        comments=comments,
        reviews=reviews,
        statuses=[_provenance("ready_for_review"), pending, passed],
    )
    _add_unsettled_prior_request(api)
    _add_current_request(api)

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
    assert result.message == "CI Gate failed because Agent Review is not successful"
    ci_writes = [payload for _, payload in api.posts if payload["context"] == CI_STATUS_CONTEXT]
    assert [payload["state"] for payload in ci_writes] == ["pending", "error"]


def test_ci_settlement_repairs_success_when_prior_request_appears_after_post() -> None:
    class PriorRequestRaceApi(FakeApi):
        injected = False

        def post(self, path: str, payload: dict[str, object]) -> object:
            value = super().post(path, payload)
            if (
                path.endswith(f"/statuses/{HEAD}")
                and payload.get("context") == CI_STATUS_CONTEXT
                and payload.get("state") == "success"
                and not self.injected
            ):
                self.injected = True
                _publish_unsettled_prior_request(self)
            return value

    boundary = _boundary("ready_for_review")
    pending = _status(
        status_id=8_003,
        context=CODEX_STATUS_CONTEXT,
        state="pending",
        description=boundary_description(boundary, "Codex pending"),
        target_url=PUBLISHER_URL,
        created_at=OUTCOME_AT,
        creator=ACTIONS_USER,
    )
    passed = _status(
        status_id=8_004,
        context=CODEX_STATUS_CONTEXT,
        state="success",
        description=boundary_description(boundary, "Codex passed"),
        target_url=PUBLISHER_URL,
        created_at=POST_REQUEST_AT,
        creator=ACTIONS_USER,
    )
    base_api, source = _api(
        action="ready_for_review",
        comments=[_clean_comment(created_at=POST_REQUEST_AT)],
        statuses=[_provenance("ready_for_review"), pending, passed],
    )
    api = PriorRequestRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        comments=base_api.comments,
    )
    _add_current_request(api)

    result = finalize(
        api,
        repository=REPOSITORY,
        source=source,
        lifecycle_workflow=LIFECYCLE_WORKFLOW_PATH,
        server_url=SERVER_URL,
        target_url=PUBLISHER_URL,
        direct_author_id=AUTHOR_ID,
    )

    assert api.injected
    assert result.message.startswith("skipped:")
    ci_writes = [
        payload
        for path, payload in api.posts
        if "/statuses/" in path and payload["context"] == CI_STATUS_CONTEXT
    ]
    assert [payload["state"] for payload in ci_writes] == [
        "pending",
        "success",
        "pending",
    ]


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

    base_api, source = _api(reactions=[_reaction(content="+1", created_at=POST_REQUEST_AT)])
    api = OutcomeRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        reactions=base_api.reactions,
    )
    _add_current_request(api)

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


@pytest.mark.parametrize("mutation", ["edit", "delete"])
def test_codex_success_is_repaired_when_exact_request_changes(mutation: str) -> None:
    class RequestRaceApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            value = super().post(path, payload)
            if (
                path.endswith(f"/statuses/{HEAD}")
                and payload.get("context") == CODEX_STATUS_CONTEXT
                and payload.get("state") == "success"
            ):
                _mutate_current_request(self, mutation)
            return value

    base_api, source = _api(comments=[_clean_comment(created_at=POST_REQUEST_AT)])
    api = RequestRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        comments=base_api.comments,
    )
    _add_current_request(api)

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

    base_api, source = _api(reactions=[_reaction(content="+1", created_at=POST_REQUEST_AT)])
    api = CloseRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        reactions=base_api.reactions,
    )
    _add_current_request(api)

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
        created_at=POST_REQUEST_AT,
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
        reactions=[_reaction(content="+1", created_at=POST_REQUEST_AT)],
        statuses=[_provenance(), _codex_pending(), passed],
    )
    api = FinalOutcomeRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        reactions=base_api.reactions,
    )
    _add_current_request(api)

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


@pytest.mark.parametrize("mutation", ["edit", "delete"])
def test_final_ci_success_is_repaired_when_exact_request_changes(mutation: str) -> None:
    passed = _status(
        status_id=8_003,
        context=CODEX_STATUS_CONTEXT,
        state="success",
        description=boundary_description(_boundary(), "Codex passed"),
        target_url=PUBLISHER_URL,
        created_at=POST_REQUEST_AT,
        creator=ACTIONS_USER,
    )

    class RequestRaceApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            value = super().post(path, payload)
            if (
                path.endswith(f"/statuses/{HEAD}")
                and payload.get("context") == CI_STATUS_CONTEXT
                and payload.get("state") == "success"
            ):
                _mutate_current_request(self, mutation)
            return value

    base_api, source = _api(
        comments=[_clean_comment(created_at=POST_REQUEST_AT)],
        statuses=[_provenance(), _codex_pending(), passed],
    )
    api = RequestRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        comments=base_api.comments,
    )
    _add_current_request(api)

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
    ci_writes = [
        payload
        for path, payload in api.posts
        if "/statuses/" in path and payload["context"] == CI_STATUS_CONTEXT
    ]
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

    base_api, source = _api(reactions=[_reaction(content="+1", created_at=POST_REQUEST_AT)])
    assert _authorize_external(base_api, source).route == "done"
    approval = next(
        comment
        for comment in base_api.comments
        if str(comment.get("body", "")).startswith("YAGA recorded maintainer approval")
    )
    _set_comment_timestamp(approval, REQUEST_AT - timedelta(seconds=1))
    api = AuthorizationRaceApi(
        current_run=base_api.current_run,
        runs=base_api.runs,
        statuses=base_api.statuses,
        reactions=base_api.reactions,
        comments=base_api.comments,
    )
    _add_current_request(api)

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
        created_at=POST_REQUEST_AT,
        creator=ACTIONS_USER,
    )
    api, source = _api(
        reactions=[_reaction(content="+1", created_at=POST_REQUEST_AT)],
        statuses=[
            _provenance(),
            _codex_pending(status_id=8_002, target_url=owner_target),
            _status(
                status_id=8_003,
                context=CODEX_STATUS_CONTEXT,
                state="pending",
                description=boundary_description(_boundary(), "Codex pending"),
                target_url=newer_target,
                created_at=POST_REQUEST_AT,
                creator=ACTIONS_USER,
            ),
            passed,
        ],
    )
    _add_current_request(api)

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
