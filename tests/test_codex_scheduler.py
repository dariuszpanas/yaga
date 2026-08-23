"""Tests for the write-capable, pending-first scheduled repair pass."""

from __future__ import annotations

import copy
import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from yaga.codex import constants, provenance, scheduler, statuses
from yaga.errors import GateError
from yaga.models import positive_int

REPOSITORY = "owner/repository"
SERVER_URL = "https://github.com"
BASE_REF = "main"
START = datetime(2026, 8, 22, 18, 0, tzinfo=UTC)
ACTIONS_USER = {
    "id": constants.GITHUB_ACTIONS_USER_ID,
    "login": constants.GITHUB_ACTIONS_LOGIN,
}


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _head(number: int) -> str:
    return f"{number:040x}"


def _base(number: int) -> str:
    return f"{10_000 + number:040x}"


def _pull_request(
    number: int,
    *,
    head: str | None = None,
    base_ref: str = BASE_REF,
) -> dict[str, object]:
    return {
        "number": number,
        "head": {
            "sha": head or _head(number),
            "ref": f"feat/pr-{number}",
            "repo": {"full_name": f"contributor-{number}/repository"},
        },
        "base": {
            "sha": _base(number),
            "ref": base_ref,
            "repo": {"full_name": REPOSITORY},
        },
        "draft": False,
        "state": "open",
        "created_at": _stamp(START - timedelta(hours=1)),
    }


def _boundary(
    number: int,
    *,
    run_id: int | None = None,
    head: str | None = None,
    occurred_at: datetime | None = None,
) -> provenance.ReviewBoundary:
    return provenance.ReviewBoundary(
        pull_request_number=number,
        head_sha=head or _head(number),
        base_sha=_base(number),
        base_ref=BASE_REF,
        occurred_at=occurred_at or START + timedelta(seconds=number),
        workflow_run_id=run_id or 100_000 + number,
    )


def _source_run(
    boundary: provenance.ReviewBoundary,
    *,
    action: str = "opened",
) -> dict[str, object]:
    title = {
        "v": 1,
        "pr": boundary.pull_request_number,
        "event": "pull_request_target",
        "head": boundary.head_sha,
        "previous": boundary.head_sha,
        "action": action,
        "base": boundary.base_sha,
        "base_ref": boundary.base_ref,
        "base_changed": False,
        "boundary": _stamp(boundary.occurred_at),
    }
    return {
        "id": boundary.workflow_run_id,
        "path": constants.DEFAULT_OBSERVER_WORKFLOW_PATH,
        "event": "pull_request_target",
        "repository": {"full_name": REPOSITORY},
        "head_sha": boundary.head_sha,
        "status": "completed",
        "display_title": json.dumps(title, separators=(",", ":")),
        "created_at": _stamp(boundary.occurred_at + timedelta(seconds=10)),
    }


def _status(
    boundary: provenance.ReviewBoundary,
    *,
    state: str = "pending",
    status_id: int | None = None,
) -> dict[str, object]:
    return {
        "id": status_id or 200_000 + boundary.pull_request_number,
        "state": state,
        "context": constants.STATUS_CONTEXT,
        "description": statuses.boundary_description(boundary, state=state),
        "target_url": (f"{SERVER_URL}/{REPOSITORY}/actions/runs/{boundary.workflow_run_id}"),
        "created_at": _stamp(boundary.occurred_at + timedelta(seconds=20)),
        "creator": copy.deepcopy(ACTIONS_USER),
    }


class ScheduleApi:
    def __init__(
        self,
        pull_requests: list[dict[str, object]],
        *,
        runs_by_head: dict[str, list[dict[str, object]]] | None = None,
        statuses_by_head: dict[str, list[dict[str, object]]] | None = None,
        totals_by_head: dict[str, int] | None = None,
    ) -> None:
        self.pull_requests = pull_requests
        self.live_pull_requests = {
            positive_int(pull_request["number"], "test pull request number"): copy.deepcopy(
                pull_request
            )
            for pull_request in pull_requests
        }
        self.runs_by_head = runs_by_head or {}
        self.statuses_by_head = statuses_by_head or {}
        self.totals_by_head = totals_by_head or {}
        self.reads: list[str] = []
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.fail_history_head: str | None = None
        self.fail_status_head: str | None = None

    def get(self, path: str) -> object:
        self.reads.append(path)
        if path == f"/repos/{REPOSITORY}":
            return {"full_name": REPOSITORY, "default_branch": BASE_REF}
        if path.startswith(f"/repos/{REPOSITORY}/pulls?state=open"):
            return copy.deepcopy(self.pull_requests)
        pull_marker = f"/repos/{REPOSITORY}/pulls/"
        if path.startswith(pull_marker):
            number = int(path.removeprefix(pull_marker))
            return copy.deepcopy(self.live_pull_requests[number])
        if "/actions/workflows/" in path:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            head = query["head_sha"][0]
            if head == self.fail_history_head:
                raise GateError("lifecycle history unavailable")
            records = copy.deepcopy(self.runs_by_head.get(head, []))
            return {
                "total_count": self.totals_by_head.get(head, len(records)),
                "workflow_runs": records,
            }
        marker = f"/repos/{REPOSITORY}/commits/"
        if path.startswith(marker) and path.endswith("/statuses?per_page=100&page=1"):
            head = path.removeprefix(marker).split("/", 1)[0]
            if head == self.fail_status_head:
                raise GateError("repair status page unavailable")
            return copy.deepcopy(self.statuses_by_head.get(head, []))
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, payload: dict[str, object]) -> object:
        marker = f"/repos/{REPOSITORY}/statuses/"
        if not path.startswith(marker):
            raise AssertionError(f"unexpected POST {path}")
        self.posts.append((path, copy.deepcopy(payload)))
        response = {
            "id": 900_000 + len(self.posts),
            "state": payload["state"],
            "context": payload["context"],
            "description": payload.get("description"),
            "target_url": payload.get("target_url"),
            "created_at": _stamp(START + timedelta(hours=2, seconds=len(self.posts))),
            "creator": copy.deepcopy(ACTIONS_USER),
        }
        head = path.removeprefix(marker)
        self.statuses_by_head.setdefault(head, []).insert(0, copy.deepcopy(response))
        return response

    def paginate(self, path: str) -> list[dict[str, Any]]:
        raise AssertionError(f"scheduled repair attempted pagination: {path}")


def _api_for(
    count: int,
    *,
    status_state: str | None,
) -> tuple[ScheduleApi, list[provenance.ReviewBoundary]]:
    pull_requests = [_pull_request(number) for number in range(1, count + 1)]
    boundaries = [_boundary(number) for number in range(1, count + 1)]
    api = ScheduleApi(
        pull_requests,
        runs_by_head={boundary.head_sha: [_source_run(boundary)] for boundary in boundaries},
        statuses_by_head=(
            {}
            if status_state is None
            else {
                boundary.head_sha: [_status(boundary, state=status_state)]
                for boundary in boundaries
            }
        ),
    )
    return api, boundaries


def test_repair_persists_every_detected_boundary_before_terminal_selection() -> None:
    api, boundaries = _api_for(6, status_state=None)

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )

    assert len(api.posts) == len(boundaries)
    assert all(payload["state"] == "pending" for _, payload in api.posts)
    assert {payload["description"] for _, payload in api.posts} == {
        statuses.boundary_description(boundary, state="pending") for boundary in boundaries
    }


def test_terminal_selection_is_capped_and_rotates_without_starvation() -> None:
    api, _ = _api_for(10, status_state="pending")

    windows = [
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001 + offset,
            now=START
            + timedelta(
                hours=1,
                minutes=constants.SCHEDULE_INTERVAL_MINUTES * offset,
            ),
        )
        for offset in range(3)
    ]

    assert all(len(window) == constants.MAX_SCHEDULE_CANDIDATES for window in windows)
    assert set(windows[0]).isdisjoint(windows[1])
    assert len({candidate for window in windows for candidate in window}) == 10
    assert api.posts == []


def test_status_read_failure_reasserts_observed_boundary_pending_and_continues() -> None:
    api, boundaries = _api_for(1, status_state="success")
    api.fail_status_head = boundaries[0].head_sha

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )

    assert [payload["state"] for _, payload in api.posts] == ["pending"]


def test_casefold_alias_success_is_replaced_by_canonical_pending() -> None:
    boundary = _boundary(1)
    canonical_pending = _status(boundary, status_id=210_001)
    alias_success = _status(boundary, state="success", status_id=210_002)
    alias_success["context"] = "CODEX REVIEW"
    alias_success["created_at"] = _stamp(boundary.occurred_at + timedelta(minutes=1))
    api = ScheduleApi(
        [_pull_request(1)],
        runs_by_head={boundary.head_sha: [_source_run(boundary)]},
        statuses_by_head={boundary.head_sha: [canonical_pending, alias_success]},
    )

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["context"] == constants.STATUS_CONTEXT
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        boundary,
        state="pending",
    )


def test_nondefault_pull_request_is_ineligible_without_a_status_write() -> None:
    pull_request = _pull_request(1, base_ref="stable")
    boundary = _boundary(1)
    api = ScheduleApi(
        [pull_request],
        runs_by_head={boundary.head_sha: [_source_run(boundary)]},
    )

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )
    assert api.posts == []
    assert not any("/actions/workflows/" in path for path in api.reads)


def test_incomplete_history_dominated_by_another_pr_is_repaired_pending() -> None:
    shared_head = _head(1)
    pull_request = _pull_request(1, head=shared_head)
    peer_boundaries = [
        _boundary(
            2,
            run_id=300_000 + index,
            head=shared_head,
            occurred_at=START + timedelta(minutes=1),
        )
        for index in range(constants.MAX_WORKFLOW_RUN_RECORDS)
    ]
    api = ScheduleApi(
        [pull_request],
        runs_by_head={shared_head: [_source_run(boundary) for boundary in peer_boundaries]},
        statuses_by_head={shared_head: [_status(_boundary(1), state="success")]},
        totals_by_head={shared_head: constants.MAX_WORKFLOW_RUN_RECORDS + 1},
    )

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.boundary_description(
        peer_boundaries[-1],
        state="pending",
    )


def test_missing_observer_source_replaces_inherited_success_with_uncertain_pending() -> None:
    pull_request = _pull_request(1)
    old_boundary = _boundary(2, head=_head(1))
    api = ScheduleApi(
        [pull_request],
        statuses_by_head={_head(1): [_status(old_boundary, state="success")]},
    )

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )

    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.UNCERTAIN_PENDING_DESCRIPTION
    assert api.posts[0][1]["target_url"] == (f"{SERVER_URL}/{REPOSITORY}/actions/runs/700001")


def test_uncertain_pending_is_idempotent_across_schedule_runs() -> None:
    api = ScheduleApi([_pull_request(1)])

    for run_id in (700_001, 700_002):
        assert (
            scheduler.repair_scheduled_boundaries(
                api,
                repository=REPOSITORY,
                server_url=SERVER_URL,
                repair_run_id=run_id,
                now=START + timedelta(hours=1),
            )
            == []
        )

    assert len(api.posts) == 1


def test_history_failure_asserts_uncertain_pending_and_continues() -> None:
    api = ScheduleApi(
        [_pull_request(1)],
        statuses_by_head={_head(1): [_status(_boundary(1), state="success")]},
    )
    api.fail_history_head = _head(1)

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )

    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.UNCERTAIN_PENDING_DESCRIPTION


def test_shared_head_cannot_preserve_a_represented_success() -> None:
    shared_head = _head(1)
    pull_requests = [_pull_request(1, head=shared_head), _pull_request(2, head=shared_head)]
    boundaries = [_boundary(number, head=shared_head) for number in (1, 2)]
    api = ScheduleApi(
        pull_requests,
        runs_by_head={shared_head: [_source_run(boundary) for boundary in boundaries]},
        statuses_by_head={shared_head: [_status(boundaries[0], state="success")]},
    )

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )
    assert [payload["state"] for _, payload in api.posts] == ["pending", "pending"]


def test_pull_request_closed_before_write_does_not_receive_post_merge_pending() -> None:
    api, _ = _api_for(1, status_state=None)
    api.live_pull_requests[1]["state"] = "closed"

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )
    assert api.posts == []


def test_future_source_asserts_uncertain_pending_and_continues() -> None:
    boundary = _boundary(1, occurred_at=START + timedelta(hours=2))
    api = ScheduleApi(
        [_pull_request(1)],
        runs_by_head={boundary.head_sha: [_source_run(boundary)]},
        statuses_by_head={boundary.head_sha: [_status(boundary, state="success")]},
    )

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )

    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.UNCERTAIN_PENDING_DESCRIPTION


@pytest.mark.parametrize("action", ["closed", "converted_to_draft"])
def test_lost_reactivation_after_ineligible_transition_cannot_inherit_success(
    action: str,
) -> None:
    current = _boundary(1)
    transition = _boundary(
        1,
        run_id=current.workflow_run_id + 1,
        occurred_at=current.occurred_at + timedelta(minutes=1),
    )
    api = ScheduleApi(
        [_pull_request(1)],
        runs_by_head={
            current.head_sha: [
                _source_run(transition, action=action),
                _source_run(current),
            ]
        },
        statuses_by_head={current.head_sha: [_status(current, state="success")]},
    )

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )
    assert [payload["state"] for _, payload in api.posts] == ["pending"]
    assert api.posts[0][1]["description"] == statuses.UNCERTAIN_PENDING_DESCRIPTION


def test_one_history_failure_does_not_skip_later_pending_repairs() -> None:
    pull_requests = [_pull_request(1), _pull_request(2)]
    second_boundary = _boundary(2)
    api = ScheduleApi(
        pull_requests,
        runs_by_head={second_boundary.head_sha: [_source_run(second_boundary)]},
        statuses_by_head={
            _head(1): [_status(_boundary(1), state="success")],
            _head(2): [_status(_boundary(2, run_id=99_002), state="success")],
        },
    )
    api.fail_history_head = _head(1)

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )

    assert [path.rsplit("/", 1)[-1] for path, _ in api.posts] == [_head(1), _head(2)]
    assert all(payload["state"] == "pending" for _, payload in api.posts)


def test_deleted_fork_record_cannot_block_an_unrelated_pending_repair() -> None:
    malformed = _pull_request(1)
    assert isinstance(malformed["head"], dict)
    malformed["head"]["repo"] = None
    second_boundary = _boundary(2)
    api = ScheduleApi(
        [malformed, _pull_request(2)],
        runs_by_head={second_boundary.head_sha: [_source_run(second_boundary)]},
        statuses_by_head={
            _head(1): [_status(_boundary(1), state="success")],
            _head(2): [_status(_boundary(2, run_id=99_002), state="success")],
        },
    )

    assert (
        scheduler.repair_scheduled_boundaries(
            api,
            repository=REPOSITORY,
            server_url=SERVER_URL,
            repair_run_id=700_001,
            now=START + timedelta(hours=1),
        )
        == []
    )

    assert [path.rsplit("/", 1)[-1] for path, _ in api.posts] == [_head(1), _head(2)]
    assert api.posts[0][1]["description"] == statuses.UNCERTAIN_PENDING_DESCRIPTION


def test_maximum_repair_pass_uses_the_modeled_163_requests() -> None:
    api, _ = _api_for(constants.MAX_OPEN_PULL_REQUESTS, status_state=None)

    scheduler.repair_scheduled_boundaries(
        api,
        repository=REPOSITORY,
        server_url=SERVER_URL,
        repair_run_id=700_001,
        now=START + timedelta(hours=1),
    )

    assert len(api.reads) + len(api.posts) == 163
