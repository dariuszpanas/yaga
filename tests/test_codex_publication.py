"""Tests for attempt-owned commit-status publication and live ownership."""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from tests.codex_support import (
    ACTIONS_USER,
    BASE_REF,
    BOUNDARY_AT,
    HEAD,
    PULL_REQUEST,
    REPOSITORY,
    TARGET_URL,
    FakeApi,
    _association,
    _pull_request,
    _status,
)
from yaga.codex import constants, publication
from yaga.errors import GateError
from yaga.github import parse_pull_request

TERMINAL_DESCRIPTION = "Trusted exact-head Codex review is current"


def _expected():
    return parse_pull_request(
        _pull_request(),
        repository=REPOSITORY,
        expected_number=PULL_REQUEST,
    )


def test_pending_lease_and_terminal_keep_one_attempt_target() -> None:
    api = FakeApi()

    lease = publication.publish_pending(
        api,
        repository=REPOSITORY,
        head_sha=HEAD,
        target_url=TARGET_URL,
    )
    assert api.reads == []
    terminal = publication.publish_terminal(
        api,
        repository=REPOSITORY,
        lease=lease,
        state="success",
        description=TERMINAL_DESCRIPTION,
    )

    assert terminal is not None
    assert publication.terminal_is_current(
        api,
        repository=REPOSITORY,
        lease=lease,
        terminal_status=terminal,
    )
    assert [payload["state"] for _, payload in api.posts] == ["pending", "success"]
    assert {payload["target_url"] for _, payload in api.posts} == {TARGET_URL}
    assert api.reads[0].endswith(f"/statuses?per_page={constants.MAX_STATUS_PAGE_RECORDS}&page=1")


def test_displaced_pending_lease_cannot_publish_terminal() -> None:
    api = FakeApi()
    lease = publication.publish_pending(
        api,
        repository=REPOSITORY,
        head_sha=HEAD,
        target_url=TARGET_URL,
    )
    api.statuses.append(
        _status(
            status_id=99_000,
            target_url=TARGET_URL.replace("/attempts/1", "/attempts/2"),
            created_at=BOUNDARY_AT + timedelta(hours=3),
        )
    )

    assert (
        publication.publish_terminal(
            api,
            repository=REPOSITORY,
            lease=lease,
            state="success",
            description=TERMINAL_DESCRIPTION,
        )
        is None
    )
    assert [payload["state"] for _, payload in api.posts] == ["pending"]


def test_case_insensitive_alias_is_replaced_with_canonical_pending() -> None:
    alias_api = FakeApi(statuses=[_status(context="codex review")])
    alias_lease = publication.StatusLease(9_001, HEAD, TARGET_URL)
    assert not publication.lease_is_current(
        alias_api,
        repository=REPOSITORY,
        lease=alias_lease,
    )
    assert (
        publication.publish_terminal(
            alias_api,
            repository=REPOSITORY,
            lease=alias_lease,
            state="success",
            description=TERMINAL_DESCRIPTION,
        )
        is None
    )
    assert [payload["state"] for _, payload in alias_api.posts] == ["pending"]
    assert alias_api.posts[-1][1]["context"] == constants.CODEX_STATUS_CONTEXT


def test_full_status_page_is_incomplete_even_with_a_visible_latest_lease() -> None:
    statuses = [
        _status(
            status_id=index + 1,
            context=f"unrelated-{index}",
            created_at=BOUNDARY_AT + timedelta(seconds=index),
        )
        for index in range(constants.MAX_STATUS_PAGE_RECORDS - 1)
    ]
    statuses.append(
        _status(
            status_id=9_001,
            created_at=BOUNDARY_AT + timedelta(hours=1),
        )
    )
    full_api = FakeApi(statuses=statuses)
    with pytest.raises(GateError, match="incomplete"):
        publication.lease_is_current(
            full_api,
            repository=REPOSITORY,
            lease=publication.StatusLease(9_001, HEAD, TARGET_URL),
        )


def test_97_existing_statuses_can_cross_to_99_with_verified_lineage() -> None:
    api = FakeApi(
        statuses=[
            _status(
                status_id=index + 1,
                context=f"unrelated-{index}",
                created_at=BOUNDARY_AT + timedelta(seconds=index),
            )
            for index in range(constants.MAX_STATUS_PAGE_RECORDS - 3)
        ]
    )
    lease = publication.publish_pending(
        api,
        repository=REPOSITORY,
        head_sha=HEAD,
        target_url=TARGET_URL,
    )
    terminal = publication.publish_terminal(
        api,
        repository=REPOSITORY,
        lease=lease,
        state="success",
        description=TERMINAL_DESCRIPTION,
    )

    assert terminal is not None
    assert len(api.statuses) == constants.MAX_STATUS_PAGE_RECORDS - 1
    assert publication.terminal_is_current(
        api,
        repository=REPOSITORY,
        lease=lease,
        terminal_status=terminal,
    )


def test_98_existing_statuses_leave_pending_without_attempting_terminal() -> None:
    class NoUnsafeWriteApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            if payload.get("state") != "pending":
                raise AssertionError("terminal publication consumed the repair slot")
            return super().post(path, payload)

    api = NoUnsafeWriteApi(
        statuses=[
            _status(
                status_id=index + 1,
                context=f"unrelated-{index}",
                created_at=BOUNDARY_AT + timedelta(seconds=index),
            )
            for index in range(constants.MAX_STATUS_PAGE_RECORDS - 2)
        ]
    )
    lease = publication.publish_pending(
        api,
        repository=REPOSITORY,
        head_sha=HEAD,
        target_url=TARGET_URL,
    )

    with pytest.raises(GateError, match="terminal and repair slot"):
        publication.publish_terminal(
            api,
            repository=REPOSITORY,
            lease=lease,
            state="success",
            description=TERMINAL_DESCRIPTION,
        )

    assert [payload["state"] for _, payload in api.posts] == ["pending"]


def test_full_page_without_a_visible_lease_is_not_authoritative() -> None:
    statuses = [
        _status(
            status_id=index + 1,
            context=f"unrelated-{index}",
            created_at=BOUNDARY_AT + timedelta(seconds=index),
        )
        for index in range(constants.MAX_STATUS_PAGE_RECORDS)
    ]
    full_api = FakeApi(statuses=statuses)
    with pytest.raises(GateError, match="incomplete"):
        publication.lease_is_current(
            full_api,
            repository=REPOSITORY,
            lease=publication.StatusLease(1, HEAD, TARGET_URL),
        )


def test_lifecycle_capacity_reserves_the_complete_context_tail() -> None:
    allowed = [
        _status(
            status_id=index + 1,
            context=constants.CODEX_STATUS_CONTEXT,
            created_at=BOUNDARY_AT + timedelta(seconds=index),
        )
        for index in range(
            constants.MAX_STATUSES_PER_CONTEXT - constants.LIFECYCLE_STATUS_SLOT_RESERVE
        )
    ]
    publication.ensure_status_capacity(
        FakeApi(statuses=allowed),
        repository=REPOSITORY,
        head_sha=HEAD,
        contexts=(constants.CODEX_STATUS_CONTEXT, constants.CI_STATUS_CONTEXT),
        reserve_per_context=constants.LIFECYCLE_STATUS_SLOT_RESERVE,
    )

    exhausted = allowed + [
        _status(
            status_id=len(allowed) + 1,
            context=constants.CODEX_STATUS_CONTEXT,
            created_at=BOUNDARY_AT + timedelta(seconds=len(allowed)),
        )
    ]
    with pytest.raises(GateError, match="lacks capacity"):
        publication.ensure_status_capacity(
            FakeApi(statuses=exhausted),
            repository=REPOSITORY,
            head_sha=HEAD,
            contexts=(constants.CODEX_STATUS_CONTEXT, constants.CI_STATUS_CONTEXT),
            reserve_per_context=constants.LIFECYCLE_STATUS_SLOT_RESERVE,
        )


def test_published_status_requires_exact_github_actions_creator() -> None:
    class LookalikeApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            status = super().post(path, payload)
            assert isinstance(status, dict)
            status["creator"] = {
                "id": ACTIONS_USER["id"],
                "login": "github-actions-lookalike[bot]",
            }
            return status

    with pytest.raises(GateError, match="unexpected creator"):
        publication.publish_pending(
            LookalikeApi(),
            repository=REPOSITORY,
            head_sha=HEAD,
            target_url=TARGET_URL,
        )


def test_accepted_success_with_malformed_response_blindly_restores_pending() -> None:
    class MalformedSuccessApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            status = super().post(path, payload)
            assert isinstance(status, dict)
            if payload.get("state") == "success":
                status["creator"] = {
                    "id": ACTIONS_USER["id"],
                    "login": "github-actions-lookalike[bot]",
                }
            return status

    api = MalformedSuccessApi()
    lease = publication.publish_pending(
        api,
        repository=REPOSITORY,
        head_sha=HEAD,
        target_url=TARGET_URL,
    )

    with pytest.raises(GateError, match="unexpected creator"):
        publication.publish_terminal(
            api,
            repository=REPOSITORY,
            lease=lease,
            state="success",
            description=TERMINAL_DESCRIPTION,
        )

    assert [payload["state"] for _, payload in api.posts] == [
        "pending",
        "success",
        "pending",
    ]
    assert api.posts[-1][1]["target_url"] == TARGET_URL


def test_status_probe_failure_blindly_reasserts_pending_before_propagating() -> None:
    class ProbeFailureApi(FakeApi):
        fail_status_probe = False

        def get(self, path: str) -> object:
            if self.fail_status_probe and "/statuses?" in path:
                raise GateError("status probe unavailable")
            return super().get(path)

    api = ProbeFailureApi()
    lease = publication.publish_pending(
        api,
        repository=REPOSITORY,
        head_sha=HEAD,
        target_url=TARGET_URL,
    )
    assert api.reads == []
    api.fail_status_probe = True

    with pytest.raises(GateError, match="status probe unavailable"):
        publication.publish_terminal(
            api,
            repository=REPOSITORY,
            lease=lease,
            state="success",
            description=TERMINAL_DESCRIPTION,
        )

    assert [payload["state"] for _, payload in api.posts] == ["pending", "pending"]


def test_live_candidate_requires_exact_ready_default_identity() -> None:
    expected = _expected()

    assert (
        publication.load_exact_live_pull_request(
            FakeApi(),
            repository=REPOSITORY,
            expected=expected,
            default_branch=BASE_REF,
            require_ready=True,
        )
        == expected
    )
    assert (
        publication.load_exact_live_pull_request(
            FakeApi(pull_request=_pull_request(draft=True)),
            repository=REPOSITORY,
            expected=expected,
            default_branch=BASE_REF,
            require_ready=True,
        )
        is None
    )
    assert (
        publication.load_exact_live_pull_request(
            FakeApi(pull_request=_pull_request(head="f" * 40)),
            repository=REPOSITORY,
            expected=expected,
            default_branch=BASE_REF,
            require_ready=True,
        )
        is None
    )


def test_unique_ownership_rejects_shared_or_truncated_associations() -> None:
    assert publication.uniquely_owned(
        FakeApi(),
        repository=REPOSITORY,
        head_sha=HEAD,
        pull_request_number=PULL_REQUEST,
    )
    assert not publication.uniquely_owned(
        FakeApi(associations=[_association(), _association(number=PULL_REQUEST + 1)]),
        repository=REPOSITORY,
        head_sha=HEAD,
        pull_request_number=PULL_REQUEST,
    )
    full = [
        _association(number=PULL_REQUEST + index, state="closed")
        for index in range(constants.MAX_HEAD_ASSOCIATIONS)
    ]
    assert not publication.uniquely_owned(
        FakeApi(associations=full),
        repository=REPOSITORY,
        head_sha=HEAD,
        pull_request_number=PULL_REQUEST,
    )


def test_raced_success_is_repaired_only_while_it_remains_latest() -> None:
    api = FakeApi()
    lease = publication.publish_pending(
        api,
        repository=REPOSITORY,
        head_sha=HEAD,
        target_url=TARGET_URL,
    )
    terminal = publication.publish_terminal(
        api,
        repository=REPOSITORY,
        lease=lease,
        state="success",
        description=TERMINAL_DESCRIPTION,
    )
    assert terminal is not None

    assert publication.restore_pending_after_race(
        api,
        repository=REPOSITORY,
        lease=lease,
        terminal_status=terminal,
    )
    assert [payload["state"] for _, payload in api.posts] == [
        "pending",
        "success",
        "pending",
    ]

    displaced = copy.deepcopy(terminal)
    api.statuses.append(
        _status(
            status_id=99_001,
            target_url=TARGET_URL.replace("/attempts/1", "/attempts/2"),
            created_at=BOUNDARY_AT + timedelta(hours=4),
        )
    )
    assert not publication.restore_pending_after_race(
        api,
        repository=REPOSITORY,
        lease=lease,
        terminal_status=displaced,
    )


def test_terminal_cannot_leapfrog_a_newer_pending_lease() -> None:
    newer_target = TARGET_URL.replace("/attempts/1", "/attempts/2")

    class PendingLeapfrogApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            if payload.get("state") == "success":
                self.statuses.append(
                    _status(
                        status_id=99_000,
                        target_url=newer_target,
                        created_at=BOUNDARY_AT + timedelta(hours=2, seconds=1),
                    )
                )
            return super().post(path, payload)

    api = PendingLeapfrogApi()
    lease = publication.publish_pending(
        api,
        repository=REPOSITORY,
        head_sha=HEAD,
        target_url=TARGET_URL,
    )
    terminal = publication.publish_terminal(
        api,
        repository=REPOSITORY,
        lease=lease,
        state="success",
        description=TERMINAL_DESCRIPTION,
    )

    assert terminal is not None
    assert not publication.terminal_is_current(
        api,
        repository=REPOSITORY,
        lease=lease,
        terminal_status=terminal,
    )
    assert publication.restore_pending_after_race(
        api,
        repository=REPOSITORY,
        lease=lease,
        terminal_status=terminal,
    )
    assert [payload["state"] for _, payload in api.posts] == [
        "pending",
        "success",
        "pending",
    ]
    assert api.posts[-1][1]["target_url"] == newer_target


def test_case_insensitive_alias_between_lease_and_terminal_forces_pending() -> None:
    alias_target = TARGET_URL.replace("/attempts/1", "/attempts/99")

    class AliasLeapfrogApi(FakeApi):
        def post(self, path: str, payload: dict[str, object]) -> object:
            if payload.get("state") == "success":
                self.statuses.append(
                    _status(
                        status_id=99_001,
                        context="codex review",
                        target_url=alias_target,
                        created_at=BOUNDARY_AT + timedelta(hours=2, seconds=1),
                    )
                )
            return super().post(path, payload)

    api = AliasLeapfrogApi()
    lease = publication.publish_pending(
        api,
        repository=REPOSITORY,
        head_sha=HEAD,
        target_url=TARGET_URL,
    )
    terminal = publication.publish_terminal(
        api,
        repository=REPOSITORY,
        lease=lease,
        state="success",
        description=TERMINAL_DESCRIPTION,
    )

    assert terminal is not None
    assert not publication.terminal_is_current(
        api,
        repository=REPOSITORY,
        lease=lease,
        terminal_status=terminal,
    )
    assert publication.restore_pending_after_race(
        api,
        repository=REPOSITORY,
        lease=lease,
        terminal_status=terminal,
    )
    assert api.posts[-1][1]["state"] == "pending"
    assert api.posts[-1][1]["target_url"] == TARGET_URL
