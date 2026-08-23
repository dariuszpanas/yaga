"""Tests for exact-candidate Codex terminal reconciliation."""

from __future__ import annotations

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
    _clean_body,
    _clean_comment,
    _formal_body,
    _legacy_statuses,
    _publish,
    _publish_candidate,
    _pull_request,
    _reaction,
    _review,
    _source_run,
    _status,
)
from yaga.codex import constants, statuses
from yaga.codex.models import Candidate
from yaga.errors import GateError


def test_exact_head_clean_comment_publishes_success_and_preserves_boundary_target() -> None:
    pending = _status()
    api = FakeApi(comments=[_clean_comment()], statuses=[pending])

    assert "trusted exact-head Codex clean comment" in _publish(api)
    assert api.posts == [
        (
            f"/repos/{REPOSITORY}/statuses/{HEAD}",
            {
                "state": "success",
                "context": constants.STATUS_CONTEXT,
                "description": statuses.boundary_description(_boundary(), state="success"),
                "target_url": pending["target_url"],
            },
        )
    ]
    assert f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/reactions" in api.reads


@pytest.mark.parametrize("action", ["reopened", "ready_for_review"])
def test_same_head_state_transition_accepts_post_boundary_exact_head_evidence(
    action: str,
) -> None:
    api = FakeApi(
        comments=[_clean_comment()],
        source_runs={RUN_ID: _source_run(action=action)},
    )

    assert "trusted exact-head Codex clean comment" in _publish(api)
    assert [payload["state"] for _, payload in api.posts] == ["success"]


def test_base_change_stays_pending_until_a_new_head_boundary() -> None:
    api = FakeApi(
        comments=[_clean_comment()],
        reviews=[_review()],
        source_runs={RUN_ID: _source_run(action="edited", base_changed=True)},
    )

    assert _publish(api).endswith("base change requires a new head before Codex evidence can pass")
    assert api.posts == []
    assert all("/comments" not in path and "/reviews" not in path for path in api.reads)


def test_initial_clean_connector_reaction_publishes_success() -> None:
    api = FakeApi(
        reactions=[_reaction(created_at=BOUNDARY_AT)],
        source_runs={RUN_ID: _source_run(action="opened")},
    )

    assert "trusted exact-head Codex clean automatic reaction" in _publish(api)
    assert [payload["state"] for _, payload in api.posts] == ["success"]


@pytest.mark.parametrize(
    ("reaction", "action"),
    [
        (_reaction(content="eyes"), "opened"),
        (
            _reaction(
                user={
                    "id": constants.CODEX_CONNECTOR_USER_ID + 1,
                    "login": constants.CODEX_CONNECTOR_LOGIN,
                }
            ),
            "opened",
        ),
        (_reaction(), "synchronize"),
        (_reaction(), "edited"),
        (_reaction(), "reopened"),
        (_reaction(), "ready_for_review"),
    ],
)
def test_clean_reaction_rejects_nonclean_stale_spoofed_or_noninitial_evidence(
    reaction: dict[str, object], action: str
) -> None:
    api = FakeApi(
        reactions=[reaction],
        source_runs={
            RUN_ID: _source_run(
                action=action,
                base_changed=action == "edited",
            )
        },
    )

    assert _publish(api).startswith("pending for ")
    assert api.posts == []
    if action in constants.AUTOMATIC_REACTION_ACTIONS:
        assert f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/reactions" in api.reads
    else:
        assert all("reactions" not in path for path in api.reads)


def test_clean_reaction_payload_fails_closed_for_exact_connector() -> None:
    api = FakeApi(reactions=[_reaction(content="invalid")])

    with pytest.raises(GateError, match="reaction content"):
        _publish(api)
    assert api.posts == []


def test_clean_reaction_ids_must_be_unique() -> None:
    api = FakeApi(reactions=[_reaction(), _reaction()])

    with pytest.raises(GateError, match="reaction ID is repeated"):
        _publish(api)
    assert api.posts == []


def test_exact_head_formal_review_is_a_completed_outcome() -> None:
    api = FakeApi(reviews=[_review()])

    assert "trusted exact-head Codex findings review" in _publish(api)
    assert [payload["state"] for _, payload in api.posts] == ["success"]


@pytest.mark.parametrize(
    "review",
    [
        _review(state="DISMISSED"),
        _review(commit_id="c" * 40),
        _review(submitted_at=BOUNDARY_AT),
        _review(body=_formal_body() + f"\n**Reviewed commit:** `{HEAD}`"),
        _review(user={"id": constants.CODEX_CONNECTOR_USER_ID, "login": "spoof[bot]"}),
    ],
)
def test_formal_review_rejects_wrong_state_head_time_marker_or_actor(
    review: dict[str, object],
) -> None:
    api = FakeApi(reviews=[review])
    assert _publish(api).startswith("pending for ")
    assert api.posts == []


def test_short_reviewed_sha_must_resolve_to_the_full_head() -> None:
    prefix = HEAD[:12]
    api = FakeApi(comments=[_clean_comment(body=_clean_body(prefix))])

    assert "clean comment" in _publish(api)
    assert f"/repos/{REPOSITORY}/commits/{prefix}" in api.reads

    wrong = FakeApi(comments=[_clean_comment(body=_clean_body(prefix))])
    wrong.get_errors[f"/repos/{REPOSITORY}/commits/{prefix}"] = GateError("ambiguous commit prefix")
    with pytest.raises(GateError, match="ambiguous"):
        _publish(wrong)
    assert wrong.posts == []


def test_missing_outcome_stays_pending_without_a_second_pending_write() -> None:
    api = FakeApi()
    assert _publish(api).startswith("pending for ")
    assert api.posts == []


def test_candidate_base_ref_is_revalidated_before_any_status_write() -> None:
    api = FakeApi(pull_request=_pull_request(base_ref="stable"))

    assert _publish(api) == "skipped: pull request does not target the repository default branch"
    assert api.posts == []


@pytest.mark.parametrize(
    ("pull_request", "default_branch", "expected_head"),
    [
        (_pull_request(draft=True), BASE_REF, HEAD),
        (_pull_request(base="c" * 40), BASE_REF, HEAD),
        (_pull_request(base_ref="stable"), "stable", HEAD),
        (_pull_request(head="d" * 40), BASE_REF, "d" * 40),
    ],
)
def test_initial_live_drift_asserts_generic_pending_before_lifecycle_history(
    pull_request: dict[str, object],
    default_branch: str,
    expected_head: str,
) -> None:
    api = FakeApi(
        pull_request=pull_request,
        statuses=[_status(state="success")],
        default_branch=default_branch,
    )

    assert _publish(api) == "pending: pull request changed before lifecycle preparation"
    assert api.posts == [
        (
            f"/repos/{REPOSITORY}/statuses/{expected_head}",
            {
                "state": "pending",
                "context": constants.STATUS_CONTEXT,
                "description": statuses.UNCERTAIN_PENDING_DESCRIPTION,
                "target_url": f"{SERVER_URL}/{REPOSITORY}/actions/runs/900001",
            },
        )
    ]
    assert all("/actions/workflows/" not in path for path in api.reads)


@pytest.mark.parametrize(
    "pull_request",
    [
        _pull_request(state="closed"),
        _pull_request(base_ref="stable"),
    ],
)
def test_initial_closed_or_nondefault_candidate_does_not_write(
    pull_request: dict[str, object],
) -> None:
    api = FakeApi(pull_request=pull_request, statuses=[_status(state="success")])

    assert _publish(api).startswith("skipped: pull request")
    assert api.posts == []
    assert all("/actions/workflows/" not in path for path in api.reads)


def test_operational_error_propagates_and_leaves_boundary_pending() -> None:
    api = FakeApi()
    comments_path = f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/comments"
    api.paginate_errors[comments_path] = GateError("temporary API failure")

    with pytest.raises(GateError, match="temporary API failure"):
        _publish(api)
    assert api.posts == []


def test_success_is_durable_without_rescanning_deleted_display_evidence() -> None:
    success = _status(state="success")
    api = FakeApi(statuses=[success])
    api.paginate_errors[f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/comments"] = AssertionError(
        "durable success must not rescan comments"
    )

    assert _publish(api) == "skipped: exact candidate already has durable Codex review success"
    assert api.posts == []


def test_stale_pending_is_upgraded_without_reusing_an_older_success() -> None:
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
    api = FakeApi(
        statuses=[semantic_success, raw_latest_pending],
        source_runs={
            newer.workflow_run_id: _source_run(newer),
            older.workflow_run_id: _source_run(older),
        },
    )

    assert _publish(api).startswith("pending for ")
    assert api.posts[0][1] == {
        "state": "pending",
        "context": constants.STATUS_CONTEXT,
        "description": statuses.boundary_description(newer, state="pending"),
        "target_url": semantic_success["target_url"],
    }


def test_semantic_newer_pending_repairs_a_raw_latest_stale_success() -> None:
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
    api = FakeApi(
        statuses=[semantic_pending, raw_latest_success],
        source_runs={
            newer.workflow_run_id: _source_run(newer),
            older.workflow_run_id: _source_run(older),
        },
    )

    assert _publish(api).startswith("pending for ")
    assert api.posts[0][1]["state"] == "pending"
    assert api.posts[0][1]["description"] == semantic_pending["description"]


def test_casefold_alias_success_is_replaced_by_canonical_pending() -> None:
    canonical_pending = _status(status_id=9_030)
    alias_success = _status(
        status_id=9_031,
        state="success",
        created_at=BOUNDARY_AT + timedelta(minutes=2),
    )
    alias_success["context"] = "codex review"
    api = FakeApi(statuses=[canonical_pending, alias_success])

    assert _publish(api).startswith("pending for ")
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["context"] == constants.STATUS_CONTEXT
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        _boundary(),
        state="pending",
    )


def test_same_timestamp_uses_run_id_as_total_order_boundary() -> None:
    lower = _boundary(run_id=RUN_ID, occurred_at=BOUNDARY_AT)
    higher = _boundary(run_id=RUN_ID + 1, occurred_at=BOUNDARY_AT)
    usage_api = FakeApi(
        statuses=[
            _status(lower, status_id=9_030, created_at=BOUNDARY_AT + timedelta(minutes=2)),
            _status(
                higher,
                status_id=9_031,
                state="success",
                created_at=BOUNDARY_AT + timedelta(minutes=1),
            ),
        ],
        source_runs={RUN_ID: _source_run(lower), RUN_ID + 1: _source_run(higher)},
    )
    usage = statuses.status_context_usage(usage_api, repository=REPOSITORY, head_sha=HEAD)

    status, boundary = statuses.authoritative_status(
        usage_api,
        usage,
        server_url=SERVER_URL,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        head_sha=HEAD,
        base_sha=BASE,
        base_ref=BASE_REF,
    ) or pytest.fail("expected authoritative boundary")
    assert status.status_id == 9_031
    assert boundary.workflow_run_id == RUN_ID + 1


def test_remaining_shared_head_owner_crosses_newer_closed_boundary() -> None:
    closed_boundary = _boundary(
        run_id=RUN_ID + 1,
        occurred_at=BOUNDARY_AT + timedelta(minutes=1),
        pull_request=PULL_REQUEST + 1,
    )
    api = FakeApi(
        associations=[
            _association(state="open"),
            _association(number=PULL_REQUEST + 1, state="closed"),
        ],
        comments=[_clean_comment()],
        statuses=[
            _status(status_id=9_001),
            _status(
                closed_boundary,
                status_id=9_002,
                created_at=BOUNDARY_AT + timedelta(minutes=1, seconds=20),
            ),
        ],
        source_runs={
            RUN_ID: _source_run(),
            closed_boundary.workflow_run_id: _source_run(closed_boundary),
        },
    )
    api.pull_requests[PULL_REQUEST + 1] = _pull_request(
        number=PULL_REQUEST + 1,
        state="closed",
    )

    assert "trusted exact-head Codex clean comment" in _publish(api)
    assert [payload["state"] for _, payload in api.posts] == ["pending", "success"]
    assert api.posts[-1][1]["description"] == statuses.boundary_description(
        _boundary(),
        state="success",
    )


def test_displaced_pending_does_not_reuse_deleted_durable_success() -> None:
    closed_boundary = _boundary(
        run_id=RUN_ID + 1,
        occurred_at=BOUNDARY_AT + timedelta(minutes=1),
        pull_request=PULL_REQUEST + 1,
    )
    api = FakeApi(
        associations=[
            _association(state="open"),
            _association(number=PULL_REQUEST + 1, state="closed"),
        ],
        statuses=[
            _status(state="success", status_id=9_001),
            _status(
                closed_boundary,
                status_id=9_002,
                created_at=BOUNDARY_AT + timedelta(minutes=1, seconds=20),
            ),
        ],
        source_runs={
            RUN_ID: _source_run(),
            closed_boundary.workflow_run_id: _source_run(closed_boundary),
        },
    )
    api.pull_requests[PULL_REQUEST + 1] = _pull_request(
        number=PULL_REQUEST + 1,
        state="closed",
    )

    assert _publish(api).startswith("pending for ")
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        _boundary(),
        state="pending",
    )


def test_displaced_semantic_race_floor_is_preserved_when_current_status_is_physically_latest() -> (
    None
):
    closed_boundary = _boundary(
        run_id=RUN_ID + 1,
        occurred_at=BOUNDARY_AT + timedelta(minutes=1),
        pull_request=PULL_REQUEST + 1,
    )
    api = FakeApi(
        associations=[
            _association(state="open"),
            _association(number=PULL_REQUEST + 1, state="closed"),
        ],
        comments=[_clean_comment(created_at=BOUNDARY_AT + timedelta(minutes=3))],
        statuses=[
            _status(
                closed_boundary,
                status_id=9_002,
                created_at=BOUNDARY_AT + timedelta(minutes=1, seconds=20),
            ),
            _status(
                status_id=9_003,
                created_at=BOUNDARY_AT + timedelta(minutes=2),
            ),
        ],
        source_runs={
            RUN_ID: _source_run(),
            closed_boundary.workflow_run_id: _source_run(closed_boundary),
        },
    )
    api.pull_requests[PULL_REQUEST + 1] = _pull_request(
        number=PULL_REQUEST + 1,
        state="closed",
    )

    assert "trusted exact-head Codex clean comment" in _publish(api)
    assert [payload["state"] for _, payload in api.posts] == ["success"]


def test_displaced_race_floor_restores_a_newer_live_boundary() -> None:
    closed_boundary = _boundary(
        run_id=RUN_ID + 1,
        occurred_at=BOUNDARY_AT + timedelta(minutes=1),
        pull_request=PULL_REQUEST + 1,
    )
    reopened = _boundary(
        run_id=RUN_ID + 2,
        occurred_at=BOUNDARY_AT + timedelta(minutes=2),
    )
    api = SuccessRaceApi(
        race_boundary=reopened,
        associations=[
            _association(state="open"),
            _association(number=PULL_REQUEST + 1, state="closed"),
        ],
        comments=[_clean_comment()],
        statuses=[
            _status(status_id=9_001),
            _status(
                closed_boundary,
                status_id=9_002,
                created_at=BOUNDARY_AT + timedelta(minutes=1, seconds=20),
            ),
        ],
        source_runs={
            RUN_ID: _source_run(),
            closed_boundary.workflow_run_id: _source_run(closed_boundary),
        },
    )
    api.pull_requests[PULL_REQUEST + 1] = _pull_request(
        number=PULL_REQUEST + 1,
        state="closed",
    )

    assert _publish(api) == "pending: a newer boundary raced Codex success publication"
    assert [payload["state"] for _, payload in api.posts] == ["pending", "success", "pending"]
    assert api.posts[-1][1]["description"] == statuses.boundary_description(
        reopened,
        state="pending",
    )


def test_displaced_boundary_is_not_crossed_while_another_pr_owns_the_head() -> None:
    api = FakeApi(
        associations=[
            _association(state="open"),
            _association(number=PULL_REQUEST + 1, state="open"),
        ],
        statuses=[_status(state="success")],
    )
    api.pull_requests[PULL_REQUEST + 1] = _pull_request(number=PULL_REQUEST + 1)

    assert _publish(api) == (
        "pending: Codex review head is not uniquely owned by the source pull request"
    )
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        _boundary(),
        state="pending",
    )


def test_draft_race_after_preparation_replaces_durable_success_with_pending() -> None:
    class DraftDuringOwnershipApi(FakeApi):
        pull_reads = 0

        def get(self, path: str) -> object:
            if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}":
                self.pull_reads += 1
                if self.pull_reads >= 2:
                    self.pull_requests[PULL_REQUEST]["draft"] = True
            return super().get(path)

    api = DraftDuringOwnershipApi(statuses=[_status(state="success")])

    assert _publish(api) == "pending: pull request state changed before ownership validation"
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        _boundary(),
        state="pending",
    )


@pytest.mark.parametrize(
    ("changes", "default_branch"),
    [
        ({"base_sha": "c" * 40}, BASE_REF),
        ({"base_ref": "stable"}, "stable"),
    ],
)
def test_same_head_base_drift_during_ownership_leaves_pending(
    changes: dict[str, str],
    default_branch: str,
) -> None:
    class BaseDriftDuringOwnershipApi(FakeApi):
        pull_reads = 0

        def get(self, path: str) -> object:
            if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}":
                self.pull_reads += 1
                if self.pull_reads >= 2:
                    base = self.pull_requests[PULL_REQUEST]["base"]
                    assert isinstance(base, dict)
                    if "base_sha" in changes:
                        base["sha"] = changes["base_sha"]
                    if "base_ref" in changes:
                        base["ref"] = changes["base_ref"]
                        self.default_branch = default_branch
            return super().get(path)

    api = BaseDriftDuringOwnershipApi(statuses=[_status(state="success")])

    assert _publish(api) == "pending: pull request state changed before ownership validation"
    assert [payload["state"] for _, payload in api.posts] == ["pending"]


def test_close_race_after_preparation_does_not_write_post_close_pending() -> None:
    class CloseDuringOwnershipApi(FakeApi):
        pull_reads = 0

        def get(self, path: str) -> object:
            if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}":
                self.pull_reads += 1
                if self.pull_reads >= 2:
                    self.pull_requests[PULL_REQUEST]["state"] = "closed"
            return super().get(path)

    api = CloseDuringOwnershipApi(statuses=[_status(state="success")])

    assert _publish(api) == "skipped: pull request closed before ownership validation"
    assert api.posts == []


def test_nondefault_race_after_preparation_does_not_write_an_ineligible_status() -> None:
    class NondefaultDuringOwnershipApi(FakeApi):
        pull_reads = 0

        def get(self, path: str) -> object:
            if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}":
                self.pull_reads += 1
                if self.pull_reads >= 2:
                    base = self.pull_requests[PULL_REQUEST]["base"]
                    assert isinstance(base, dict)
                    base["ref"] = "stable"
            return super().get(path)

    api = NondefaultDuringOwnershipApi(statuses=[_status(state="success")])

    assert _publish(api) == (
        "skipped: pull request no longer targets the repository default branch"
    )
    assert api.posts == []


def test_new_head_race_asserts_uncertain_pending_on_the_live_sha() -> None:
    new_head = "d" * 40

    class NewHeadDuringOwnershipApi(FakeApi):
        pull_reads = 0

        def get(self, path: str) -> object:
            if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}":
                self.pull_reads += 1
                if self.pull_reads >= 2:
                    head = self.pull_requests[PULL_REQUEST]["head"]
                    assert isinstance(head, dict)
                    head["sha"] = new_head
            return super().get(path)

    api = NewHeadDuringOwnershipApi(statuses=[_status(state="success")])

    assert _publish(api) == "pending: pull request head changed before ownership validation"
    assert api.posts == [
        (
            f"/repos/{REPOSITORY}/statuses/{new_head}",
            {
                "state": "pending",
                "context": constants.STATUS_CONTEXT,
                "description": statuses.UNCERTAIN_PENDING_DESCRIPTION,
                "target_url": f"{SERVER_URL}/{REPOSITORY}/actions/runs/900001",
            },
        )
    ]


def test_owner_request_comment_is_not_outcome_evidence_or_a_write() -> None:
    owner_request = _clean_comment(
        body="@codex review",
        user={"id": 50_001, "login": "repository-owner"},
    )
    api = FakeApi(comments=[owner_request])
    assert _publish(api).startswith("pending for ")
    assert api.posts == []
    assert f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/reactions" in api.reads


def test_targeted_wake_recovers_a_newer_boundary_then_reconciles_evidence() -> None:
    missed = _boundary(
        run_id=RUN_ID + 1,
        occurred_at=BOUNDARY_AT + timedelta(minutes=2),
    )
    api = FakeApi(
        comments=[_clean_comment(created_at=missed.occurred_at + timedelta(minutes=1))],
        source_runs={
            RUN_ID: _source_run(),
            missed.workflow_run_id: _source_run(missed, action="synchronize"),
        },
    )

    assert _publish(api).startswith("success for ")
    assert [payload["state"] for _, payload in api.posts] == ["pending", "success"]
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        missed,
        state="pending",
    )
    assert any("/comments" in path for path in api.reads)


def test_targeted_wake_recovers_a_base_changed_boundary_before_terminal_evidence() -> None:
    changed_base = "c" * 40
    missed = _boundary(
        base=changed_base,
        run_id=RUN_ID + 1,
        occurred_at=BOUNDARY_AT + timedelta(minutes=2),
    )
    api = FakeApi(
        pull_request=_pull_request(base=changed_base),
        statuses=[_status(state="success")],
        source_runs={
            RUN_ID: _source_run(),
            missed.workflow_run_id: _source_run(missed, action="edited", base_changed=True),
        },
    )
    candidate = Candidate(
        pull_request_number=PULL_REQUEST,
        head_sha=HEAD,
        base_sha=changed_base,
        base_ref=BASE_REF,
        head_repository=HEAD_REPOSITORY,
        head_ref=HEAD_REF,
    )

    assert _publish_candidate(api, candidate).endswith(
        "base change requires a new head before Codex evidence can pass"
    )
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        missed,
        state="pending",
    )


def test_targeted_wake_upgrades_generic_pending_to_exact_authority() -> None:
    generic_pending = _status(
        description=statuses.UNCERTAIN_PENDING_DESCRIPTION,
        target_url=f"{SERVER_URL}/{REPOSITORY}/actions/runs/900001",
    )
    api = FakeApi(comments=[_clean_comment()], statuses=[generic_pending])

    assert _publish(api).startswith("success for ")
    assert [payload["state"] for _, payload in api.posts] == ["pending", "success"]
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        _boundary(),
        state="pending",
    )


def test_targeted_wake_without_a_source_asserts_uncertain_pending() -> None:
    api = FakeApi(statuses=_legacy_statuses(1))
    api.source_runs = {}

    assert _publish(api) == "pending: no trusted lifecycle boundary is available"
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.UNCERTAIN_PENDING_DESCRIPTION


def test_targeted_wake_history_failure_asserts_uncertain_pending_before_error() -> None:
    class HistoryFailureApi(FakeApi):
        def get(self, path: str) -> object:
            if "/actions/workflows/" in path:
                raise GateError("targeted lifecycle history unavailable")
            return super().get(path)

    api = HistoryFailureApi(statuses=_legacy_statuses(1))

    with pytest.raises(GateError, match="targeted lifecycle history unavailable"):
        _publish(api)

    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.UNCERTAIN_PENDING_DESCRIPTION


def test_targeted_status_probe_failure_asserts_source_pending_before_error() -> None:
    api = FakeApi(statuses=[_status(state="success")])
    status_path = (
        f"/repos/{REPOSITORY}/commits/{HEAD}/statuses?per_page="
        f"{constants.MAX_STATUS_PAGE_RECORDS}&page=1"
    )
    api.get_errors[status_path] = GateError("targeted status probe unavailable")

    with pytest.raises(GateError, match="targeted status probe unavailable"):
        _publish(api)

    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        _boundary(),
        state="pending",
    )


def test_targeted_history_and_status_failure_still_attempts_uncertain_pending() -> None:
    class DoubleFailureApi(FakeApi):
        def get(self, path: str) -> object:
            if "/actions/workflows/" in path:
                raise GateError("targeted lifecycle history unavailable")
            if "/statuses?per_page=" in path:
                raise GateError("targeted status probe unavailable")
            return super().get(path)

    api = DoubleFailureApi(statuses=[_status(state="success")])

    with pytest.raises(GateError, match="pending repair also failed"):
        _publish(api)

    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.UNCERTAIN_PENDING_DESCRIPTION


@pytest.mark.parametrize("failure", ["omitted", "api"])
def test_success_wake_restores_pending_when_lifecycle_history_is_uncertain(
    failure: str,
) -> None:
    class HistoryFailureApi(FakeApi):
        def get(self, path: str) -> object:
            if "/actions/workflows/" in path:
                raise GateError("lifecycle history unavailable")
            return super().get(path)

    api = (
        FakeApi(statuses=[_status(state="success")])
        if failure == "omitted"
        else HistoryFailureApi(statuses=[_status(state="success")])
    )
    if failure == "omitted":
        api.workflow_history = {"total_count": 0, "workflow_runs": []}

    if failure == "omitted":
        assert _publish(api) == "pending: no trusted lifecycle boundary is available"
    else:
        with pytest.raises(GateError, match="history"):
            _publish(api)

    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.UNCERTAIN_PENDING_DESCRIPTION
