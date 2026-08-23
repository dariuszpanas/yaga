"""Tests for pending-first Codex status publication and race repair."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests.codex_support import (
    BASE,
    BASE_REF,
    BOUNDARY_AT,
    HEAD,
    HEAD_REF,
    HEAD_REPOSITORY,
    PULL_REQUEST,
    REPOSITORY,
    RUN_ID,
    SERVER_URL,
    FakeApi,
    SuccessRaceApi,
    _association,
    _boundary,
    _clean_comment,
    _legacy_statuses,
    _lifecycle_workflow_run_event,
    _publish,
    _pull_request,
    _source_run,
    _status,
)
from yaga.codex import candidates, constants, provenance, publication, publisher, statuses
from yaga.codex.models import Candidate
from yaga.errors import GateError


def test_status_publication_requires_github_to_echo_the_exact_payload() -> None:
    class MismatchedStatusApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            response = super().post(path, payload)
            assert isinstance(response, dict)
            response["target_url"] = f"{SERVER_URL}/{REPOSITORY}/actions/runs/1"
            return response

    with pytest.raises(GateError, match="different Codex review status"):
        publication.publish_pending_boundary(
            MismatchedStatusApi(),
            repository=REPOSITORY,
            head_sha=HEAD,
            boundary=_boundary(),
            target_url=f"{SERVER_URL}/{REPOSITORY}/actions/runs/{RUN_ID}",
        )


def test_status_change_before_final_publication_does_not_overwrite_newer_writer() -> None:
    pending = _status()
    newer = _boundary(run_id=RUN_ID + 1, occurred_at=BOUNDARY_AT + timedelta(minutes=2))
    newer_pending = _status(newer, status_id=9_099, created_at=BOUNDARY_AT + timedelta(minutes=3))
    api = FakeApi(
        comments=[_clean_comment()],
        statuses=[pending],
        source_runs={RUN_ID: _source_run(), RUN_ID + 1: _source_run(newer)},
    )
    api.status_snapshots = [[pending], [pending, newer_pending]]

    assert _publish(api) == "pending: a newer lifecycle boundary was recovered"
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        newer,
        state="pending",
    )


def test_success_post_reasserts_a_newer_pending_boundary_that_raced_the_write() -> None:
    newer = _boundary(run_id=RUN_ID + 1, occurred_at=BOUNDARY_AT + timedelta(minutes=2))
    filler = _legacy_statuses(constants.MAX_STATUSES_BEFORE_TERMINAL_WRITE - 2)
    api = SuccessRaceApi(
        race_boundary=newer,
        comments=[_clean_comment()],
        statuses=[*filler, _status(status_id=10_500)],
    )

    assert _publish(api) == "pending: a newer boundary raced Codex success publication"
    assert [payload["state"] for _, payload in api.posts] == ["success", "pending"]
    assert len(api.statuses) == constants.MAX_STATUSES_BEFORE_TERMINAL_WRITE + 2
    assert api.posts[-1][1]["description"] == statuses.boundary_description(
        newer,
        state="pending",
    )


def test_success_post_reasserts_a_newer_pending_boundary_for_a_changed_base() -> None:
    newer = _boundary(
        base="c" * 40,
        run_id=RUN_ID + 1,
        occurred_at=BOUNDARY_AT + timedelta(minutes=2),
    )
    api = SuccessRaceApi(
        race_boundary=newer,
        comments=[_clean_comment()],
        statuses=[_status(status_id=10_500)],
    )

    assert _publish(api) == "pending: a newer boundary raced Codex success publication"
    assert [payload["state"] for _, payload in api.posts] == ["success", "pending"]
    assert api.posts[-1][1]["description"] == statuses.boundary_description(
        newer,
        state="pending",
    )


def test_success_post_reasserts_a_newer_pending_boundary_for_a_new_owner() -> None:
    newer = _boundary(
        pull_request=PULL_REQUEST + 1,
        run_id=RUN_ID + 1,
        occurred_at=BOUNDARY_AT + timedelta(minutes=2),
    )
    api = SuccessRaceApi(
        race_boundary=newer,
        comments=[_clean_comment()],
        statuses=[_status(status_id=10_500)],
    )

    assert _publish(api) == "pending: a newer boundary raced Codex success publication"
    assert [payload["state"] for _, payload in api.posts] == ["success", "pending"]
    assert api.posts[-1][1]["description"] == statuses.boundary_description(
        newer,
        state="pending",
    )


def test_post_success_read_failure_best_effort_restores_pending() -> None:
    class StatusReadFailureApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            record = super().post(path, payload)
            if payload.get("state") == "success":
                status_path = (
                    f"/repos/{REPOSITORY}/commits/{HEAD}/statuses?per_page="
                    f"{constants.MAX_STATUS_PAGE_RECORDS}&page=1"
                )
                self.get_errors[status_path] = GateError("post-success status read failed")
            return record

    api = StatusReadFailureApi(comments=[_clean_comment()])

    with pytest.raises(GateError, match="post-success status read failed"):
        _publish(api)

    assert [payload["state"] for _, payload in api.posts] == ["success", "pending"]
    assert api.posts[-1][1]["description"] == statuses.boundary_description(
        _boundary(),
        state="pending",
    )


def test_post_success_status_omission_best_effort_restores_pending() -> None:
    api = FakeApi()
    api.status_snapshots = [[]]
    pull_request = candidates.open_owned_candidate(
        api,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        expected_head=HEAD,
        expected_base_sha=BASE,
        expected_base_ref=BASE_REF,
        expected_head_repository=HEAD_REPOSITORY,
        expected_head_ref=HEAD_REF,
    )
    assert pull_request is not None

    with pytest.raises(GateError, match="absent from the latest status page"):
        publication.publish_success_with_pending_repair(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            pull_request=pull_request,
            boundary=_boundary(),
            target_url=f"{SERVER_URL}/{REPOSITORY}/actions/runs/{RUN_ID}",
            boundary_cache={},
        )

    assert [payload["state"] for _, payload in api.posts] == ["success", "pending"]


def test_success_is_not_posted_without_the_complete_local_tail_reserve() -> None:
    class ExhaustedApi(FakeApi):
        requests_remaining = constants.SUCCESS_WRITE_REQUEST_RESERVE - 1

        def require_success_tail(self, count: int, *, cleanup_margin_seconds: int) -> None:
            assert count == constants.SUCCESS_WRITE_REQUEST_RESERVE
            assert cleanup_margin_seconds == constants.SUCCESS_CLEANUP_MARGIN_SECONDS
            if count > self.requests_remaining:
                raise GateError("GitHub API request reserve is unavailable")

    api = ExhaustedApi(comments=[_clean_comment()])

    with pytest.raises(GateError, match="reserve is unavailable"):
        _publish(api)

    assert api.posts == []


def test_success_is_not_posted_after_the_local_job_cutoff() -> None:
    class CutoffApi(FakeApi):
        def require_success_tail(self, count: int, *, cleanup_margin_seconds: int) -> None:
            assert count == constants.SUCCESS_WRITE_REQUEST_RESERVE
            assert cleanup_margin_seconds == constants.SUCCESS_CLEANUP_MARGIN_SECONDS
            raise GateError("success validation tail no longer fits the job deadline")

    api = CutoffApi(comments=[_clean_comment()])

    with pytest.raises(GateError, match="job deadline"):
        _publish(api)

    assert api.posts == []


def test_success_and_its_bounded_validation_tail_fit_the_reserved_requests() -> None:
    class TailCountingApi(FakeApi):
        tail_requests: int | None = None

        def require_success_tail(self, count: int, *, cleanup_margin_seconds: int) -> None:
            assert count == constants.SUCCESS_WRITE_REQUEST_RESERVE
            assert cleanup_margin_seconds == constants.SUCCESS_CLEANUP_MARGIN_SECONDS
            self.tail_requests = 0

        def _count_tail(self) -> None:
            if self.tail_requests is not None:
                self.tail_requests += 1
                assert self.tail_requests <= constants.SUCCESS_WRITE_REQUEST_RESERVE

        def get(self, path: str) -> object:
            self._count_tail()
            return super().get(path)

        def post(self, path: str, payload: dict[str, object]) -> object:
            self._count_tail()
            return super().post(path, payload)

    api = TailCountingApi(comments=[_clean_comment()])

    assert _publish(api).startswith("success for ")
    assert api.tail_requests is not None
    assert api.tail_requests <= constants.SUCCESS_WRITE_REQUEST_RESERVE


def test_success_error_tail_with_source_validation_and_fallback_fits_reserve() -> None:
    current = _boundary()
    raced = _boundary(run_id=RUN_ID + 1, occurred_at=BOUNDARY_AT + timedelta(minutes=2))

    class WorstTailApi(FakeApi):
        tail_requests: int | None = None
        malformed_repair_returned = False

        def require_success_tail(self, count: int, *, cleanup_margin_seconds: int) -> None:
            assert count == constants.SUCCESS_WRITE_REQUEST_RESERVE
            assert cleanup_margin_seconds == constants.SUCCESS_CLEANUP_MARGIN_SECONDS
            self.tail_requests = 0

        def _count_tail(self) -> None:
            if self.tail_requests is not None:
                self.tail_requests += 1
                assert self.tail_requests <= constants.SUCCESS_WRITE_REQUEST_RESERVE

        def get(self, path: str) -> object:
            self._count_tail()
            return super().get(path)

        def post(self, path: str, payload: dict[str, object]) -> object:
            self._count_tail()
            record = super().post(path, payload)
            if payload.get("state") == "success":
                self.statuses.append(
                    _status(
                        raced,
                        status_id=20_001,
                        state="success",
                        created_at=BOUNDARY_AT + timedelta(hours=3),
                    )
                )
            elif not self.malformed_repair_returned:
                self.malformed_repair_returned = True
                assert isinstance(record, dict)
                return {"id": record["id"]}
            return record

    api = WorstTailApi(
        comments=[_clean_comment()],
        source_runs={
            current.workflow_run_id: _source_run(current),
            raced.workflow_run_id: _source_run(raced, action="synchronize"),
        },
        workflow_history={
            "total_count": 1,
            "workflow_runs": [_source_run(current)],
        },
    )

    with pytest.raises(GateError, match="different Codex review status"):
        _publish(api)

    assert [payload["state"] for _, payload in api.posts] == [
        "success",
        "pending",
        "pending",
    ]
    assert api.tail_requests == 8
    assert api.tail_requests <= constants.SUCCESS_WRITE_REQUEST_RESERVE
    assert any(
        path == f"/repos/{REPOSITORY}/actions/runs/{raced.workflow_run_id}" for path in api.reads
    )


def test_ambiguous_success_response_is_compensated_with_pending() -> None:
    class AmbiguousSuccessApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            record = super().post(path, payload)
            if payload.get("state") == "success":
                return {"id": record["id"]} if isinstance(record, dict) else {}
            return record

    api = AmbiguousSuccessApi(comments=[_clean_comment()])

    with pytest.raises(GateError, match="different Codex review status"):
        _publish(api)

    assert [payload["state"] for _, payload in api.posts] == ["success", "pending"]
    assert api.posts[-1][1]["description"] == statuses.boundary_description(
        _boundary(),
        state="pending",
    )


def test_post_success_race_has_a_reserved_source_validation_slot() -> None:
    current = _boundary()
    newer = _boundary(run_id=RUN_ID + 1, occurred_at=BOUNDARY_AT + timedelta(minutes=2))
    boundary_cache: dict[int, provenance.ReviewBoundary | None] = {current.workflow_run_id: current}
    boundary_cache.update(
        {1_000_000 + index: None for index in range(constants.MAX_SOURCE_BOUNDARY_VALIDATIONS - 1)}
    )
    api = SuccessRaceApi(race_boundary=newer)
    pull_request = candidates.open_owned_candidate(
        api,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        expected_head=HEAD,
        expected_base_sha=BASE,
        expected_base_ref=BASE_REF,
        expected_head_repository=HEAD_REPOSITORY,
        expected_head_ref=HEAD_REF,
    )
    assert pull_request is not None

    assert publication.publish_success_with_pending_repair(
        api,
        repository=REPOSITORY,
        server_url=SERVER_URL,
        pull_request=pull_request,
        boundary=current,
        target_url=f"{SERVER_URL}/{REPOSITORY}/actions/runs/{RUN_ID}",
        boundary_cache=boundary_cache,
    )

    assert len(boundary_cache) == constants.MAX_SOURCE_BOUNDARY_VALIDATIONS + 1
    assert [payload["state"] for _, payload in api.posts] == ["success", "pending"]
    assert newer.workflow_run_id in boundary_cache
    assert any("/actions/workflows/" in path for path in api.reads)


def test_conservative_invalidation_buffer_can_fill_context_with_pending_latest() -> None:
    race_boundaries = tuple(
        _boundary(
            run_id=RUN_ID + index + 1,
            occurred_at=BOUNDARY_AT + timedelta(seconds=index + 1),
        )
        for index in range(constants.MAX_BUFFERED_INVALIDATIONS)
    )
    filler = _legacy_statuses(constants.MAX_STATUSES_BEFORE_TERMINAL_WRITE - 2)
    api = SuccessRaceApi(
        race_boundaries=race_boundaries,
        comments=[_clean_comment()],
        statuses=[*filler, _status(status_id=10_500)],
    )

    assert _publish(api) == "pending: a newer boundary raced Codex success publication"
    assert [payload["state"] for _, payload in api.posts] == ["success", "pending"]
    assert len(api.statuses) == constants.MAX_STATUSES_PER_CONTEXT
    assert api.posts[-1][1]["description"] == statuses.boundary_description(
        race_boundaries[-1],
        state="pending",
    )


def test_terminal_ceiling_prevents_success_at_count_100() -> None:
    pending = _status(status_id=10_500, created_at=BOUNDARY_AT + timedelta(hours=1))
    filler = _legacy_statuses(constants.MAX_STATUSES_BEFORE_TERMINAL_WRITE - 1)
    for status in filler:
        status["context"] = "codex review"
    api = FakeApi(comments=[_clean_comment()], statuses=[*filler, pending])

    with pytest.raises(GateError, match="capacity"):
        _publish(api)
    assert api.posts == []


def test_stale_order_at_terminal_ceiling_is_repaired_only_to_pending() -> None:
    newer = _boundary(run_id=RUN_ID + 2, occurred_at=BOUNDARY_AT + timedelta(minutes=2))
    older = _boundary(run_id=RUN_ID + 1, occurred_at=BOUNDARY_AT + timedelta(minutes=1))
    semantic_success = _status(
        newer,
        status_id=9_010,
        state="success",
        created_at=BOUNDARY_AT + timedelta(minutes=3),
    )
    raw_latest_pending = _status(
        older,
        status_id=9_011,
        created_at=BOUNDARY_AT + timedelta(minutes=4),
    )
    filler = _legacy_statuses(constants.MAX_STATUSES_BEFORE_TERMINAL_WRITE - 2)
    api = FakeApi(
        statuses=[*filler, semantic_success, raw_latest_pending],
        source_runs={
            newer.workflow_run_id: _source_run(newer),
            older.workflow_run_id: _source_run(older),
        },
    )

    assert _publish(api).startswith("pending for ")
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert len(api.statuses) == constants.MAX_STATUSES_BEFORE_TERMINAL_WRITE + 1
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        newer,
        state="pending",
    )


def test_pending_order_repair_can_use_the_final_status_slot_at_count_999() -> None:
    newer = _boundary(run_id=RUN_ID + 2, occurred_at=BOUNDARY_AT + timedelta(minutes=2))
    older = _boundary(run_id=RUN_ID + 1, occurred_at=BOUNDARY_AT + timedelta(minutes=1))
    semantic_pending = _status(
        newer,
        status_id=9_020,
        created_at=BOUNDARY_AT + timedelta(minutes=3),
    )
    raw_latest_success = _status(
        older,
        status_id=9_021,
        state="success",
        created_at=BOUNDARY_AT + timedelta(minutes=4),
    )
    filler = _legacy_statuses(constants.MAX_STATUSES_PER_CONTEXT - 3)
    api = FakeApi(
        statuses=[*filler, semantic_pending, raw_latest_success],
        source_runs={
            newer.workflow_run_id: _source_run(newer),
            older.workflow_run_id: _source_run(older),
        },
    )

    assert _publish(api).startswith("pending for ")
    assert len(api.statuses) == constants.MAX_STATUSES_PER_CONTEXT
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == semantic_pending["description"]
    assert api.reads.count(f"/repos/{REPOSITORY}/commits/{HEAD}/statuses") == 1


def test_invalidation_records_one_idempotent_pending_boundary() -> None:
    source = _source_run(action="opened")
    api = FakeApi(statuses=[], source_runs={RUN_ID: source})
    event = _lifecycle_workflow_run_event(source)

    candidate = publisher.invalidate_lifecycle_event(
        api,
        repository=REPOSITORY,
        server_url=SERVER_URL,
        event_name="workflow_run",
        event=event,
    )

    assert candidate == Candidate(
        pull_request_number=PULL_REQUEST,
        head_sha=HEAD,
        base_sha=BASE,
        base_ref=BASE_REF,
        head_repository=HEAD_REPOSITORY,
        head_ref=HEAD_REF,
    )
    assert len(api.posts) == 1
    assert api.posts[0][1]["state"] == "pending"
    assert api.posts[0][1]["context"] == constants.STATUS_CONTEXT
    assert (
        publisher.invalidate_lifecycle_event(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            event_name="workflow_run",
            event=event,
        )
        == candidate
    )
    assert len(api.posts) == 1


@pytest.mark.parametrize("failure", ["probe", "history"])
def test_invalidation_posts_pending_before_status_read_failure(failure: str) -> None:
    source = _source_run(action="reopened")
    api = FakeApi(statuses=[_status(state="success")], source_runs={RUN_ID: source})
    probe_path = (
        f"/repos/{REPOSITORY}/commits/{HEAD}/statuses?per_page="
        f"{constants.MAX_STATUS_PAGE_RECORDS}&page=1"
    )
    history_path = f"/repos/{REPOSITORY}/commits/{HEAD}/statuses"
    if failure == "probe":
        api.get_errors[probe_path] = GateError("status probe unavailable")
    else:
        api.paginate_errors[history_path] = GateError("status history unavailable")

    with pytest.raises(GateError, match="status"):
        publisher.invalidate_lifecycle_event(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            event_name="workflow_run",
            event=_lifecycle_workflow_run_event(source),
        )

    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        _boundary(),
        state="pending",
    )


@pytest.mark.parametrize(
    ("action", "base_changed"),
    [
        ("closed", False),
        ("edited", False),
    ],
)
def test_irrelevant_observer_actions_do_not_write_status(
    action: str,
    base_changed: bool,
) -> None:
    source = _source_run(action=action, base_changed=base_changed)
    api = FakeApi(source_runs={RUN_ID: source})
    api.statuses = []

    assert (
        publisher.invalidate_lifecycle_event(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            event_name="workflow_run",
            event=_lifecycle_workflow_run_event(source),
        )
        is None
    )
    assert api.posts == []
    assert not any(path.endswith("/statuses") for path in api.reads)


def test_converted_to_draft_persists_pending_without_terminal_candidate() -> None:
    source = _source_run(action="converted_to_draft")
    api = FakeApi(
        pull_request=_pull_request(draft=True),
        statuses=[_status(state="success")],
        source_runs={RUN_ID: source},
    )

    assert (
        publisher.invalidate_lifecycle_event(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            event_name="workflow_run",
            event=_lifecycle_workflow_run_event(source),
        )
        is None
    )
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        _boundary(),
        state="pending",
    )
    assert all("/comments" not in path and "/reviews" not in path for path in api.reads)


def test_invalidation_rejects_a_malformed_observer_title() -> None:
    source = _source_run()
    title = json.loads(str(source["display_title"]))
    title["v"] = 2
    source["display_title"] = json.dumps(title, separators=(",", ":"))

    with pytest.raises(GateError, match="title fields"):
        publisher.invalidate_lifecycle_event(
            FakeApi(source_runs={RUN_ID: source}),
            repository=REPOSITORY,
            server_url=SERVER_URL,
            event_name="workflow_run",
            event=_lifecycle_workflow_run_event(source),
        )


def test_lifecycle_event_and_candidate_json_preserve_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Candidate] = []
    source = _source_run()
    source["actor"] = {"id": 49_639_933, "login": "dependabot[bot]"}

    def fake_publish_candidate(*_: object, candidate: Candidate, **__: object) -> str:
        captured.append(candidate)
        return "published"

    monkeypatch.setattr(publisher, "reconcile_candidate", fake_publish_candidate)
    assert (
        publisher.reconcile_lifecycle_event(
            FakeApi(source_runs={RUN_ID: source}),
            repository=REPOSITORY,
            server_url=SERVER_URL,
            event_name="workflow_run",
            event=_lifecycle_workflow_run_event(source),
            reconciliation_run_id=900_001,
        )
        == "published"
    )
    candidate = captured[0]
    assert Candidate.from_json(candidate.to_json()) == candidate


def test_metadata_edit_and_nondefault_base_are_not_lifecycle_boundaries() -> None:
    metadata_source = _source_run(action="edited", base_changed=False)
    with pytest.raises(GateError, match="not a review boundary"):
        publisher.reconcile_lifecycle_event(
            FakeApi(source_runs={RUN_ID: metadata_source}),
            repository=REPOSITORY,
            server_url=SERVER_URL,
            event_name="workflow_run",
            event=_lifecycle_workflow_run_event(metadata_source),
            reconciliation_run_id=900_001,
        )

    source = _source_run()
    nondefault = _lifecycle_workflow_run_event(source)
    nondefault["repository"] = {"default_branch": "stable", "full_name": REPOSITORY}
    assert publisher.reconcile_lifecycle_event(
        FakeApi(source_runs={RUN_ID: source}),
        repository=REPOSITORY,
        server_url=SERVER_URL,
        event_name="workflow_run",
        event=nondefault,
        reconciliation_run_id=900_001,
    ).startswith("skipped:")

    api = FakeApi(source_runs={RUN_ID: source})
    api.statuses = []
    assert (
        publisher.invalidate_lifecycle_event(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            event_name="workflow_run",
            event=nondefault,
        )
        is None
    )
    assert api.posts == []


def test_invalidation_persists_pending_before_shared_head_ownership() -> None:
    source = _source_run()
    api = FakeApi(
        associations=[
            _association(),
            _association(number=PULL_REQUEST + 1),
        ],
        statuses=[_status(state="success")],
        source_runs={RUN_ID: source},
    )
    api.pull_requests[PULL_REQUEST + 1] = _pull_request(number=PULL_REQUEST + 1)

    assert (
        publisher.invalidate_lifecycle_event(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            event_name="workflow_run",
            event=_lifecycle_workflow_run_event(source),
        )
        is None
    )
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert all("/comments" not in path and "/reviews" not in path for path in api.reads)


def test_closed_event_never_reads_a_deleted_fork_pull_request() -> None:
    source = _source_run(action="closed")
    api = FakeApi(source_runs={RUN_ID: source})
    pull_path = f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}"
    api.get_errors[pull_path] = GateError("deleted fork pull request is unavailable")

    event = _lifecycle_workflow_run_event(source)
    assert (
        publisher.invalidate_lifecycle_event(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            event_name="workflow_run",
            event=event,
        )
        is None
    )
    assert publisher.reconcile_lifecycle_event(
        api,
        repository=REPOSITORY,
        server_url=SERVER_URL,
        event_name="workflow_run",
        event=event,
        reconciliation_run_id=900_001,
    ).startswith("skipped:")
    assert pull_path not in api.reads
    assert api.posts == []


def test_invalidation_posts_pending_with_more_than_the_source_validation_budget() -> None:
    old_boundaries = [
        _boundary(
            run_id=RUN_ID + index,
            occurred_at=BOUNDARY_AT + timedelta(seconds=index),
        )
        for index in range(constants.MAX_SOURCE_BOUNDARY_VALIDATIONS)
    ]
    newest = _boundary(
        run_id=RUN_ID + 10_000,
        occurred_at=BOUNDARY_AT + timedelta(minutes=5),
    )
    api = FakeApi(
        statuses=[
            _status(
                boundary,
                status_id=20_000 + index,
                state="success",
                created_at=boundary.occurred_at + timedelta(seconds=20),
            )
            for index, boundary in enumerate(old_boundaries)
        ],
        source_runs={
            **{boundary.workflow_run_id: _source_run(boundary) for boundary in old_boundaries},
            newest.workflow_run_id: _source_run(newest, action="synchronize"),
        },
    )

    candidate = publisher.invalidate_lifecycle_event(
        api,
        repository=REPOSITORY,
        server_url=SERVER_URL,
        event_name="workflow_run",
        event=_lifecycle_workflow_run_event(api.source_runs[newest.workflow_run_id]),
    )

    assert candidate is not None
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[-1][1]["description"] == statuses.boundary_description(
        newest,
        state="pending",
    )


def test_second_head_owner_opening_during_success_restores_pending() -> None:
    class OwnershipRaceApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            record = super().post(path, payload)
            if payload.get("state") == "success":
                self.pull_requests[PULL_REQUEST + 1] = _pull_request(number=PULL_REQUEST + 1)
                self.associations = [
                    _association(),
                    _association(number=PULL_REQUEST + 1),
                ]
            return record

    api = OwnershipRaceApi(comments=[_clean_comment()])

    assert _publish(api) == "pending: a newer boundary raced Codex success publication"
    assert [payload["state"] for _, payload in api.posts] == ["success", "pending"]


def test_zero_open_head_owners_after_success_does_not_restore_pending() -> None:
    class CloseRaceApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            record = super().post(path, payload)
            if payload.get("state") == "success":
                self.pull_requests[PULL_REQUEST]["state"] = "closed"
                self.associations = [_association(state="closed")]
            return record

    api = CloseRaceApi(comments=[_clean_comment()])

    assert _publish(api).startswith("success for ")
    assert [payload["state"] for _, payload in api.posts] == ["success"]
