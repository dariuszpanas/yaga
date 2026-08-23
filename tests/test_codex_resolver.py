from __future__ import annotations

import copy
import json

import pytest

from yaga.codex import resolver
from yaga.codex.constants import (
    CODEX_CONNECTOR_LOGIN,
    CODEX_CONNECTOR_USER_ID,
    DEFAULT_OBSERVER_WORKFLOW_PATH,
)
from yaga.codex.models import Candidate
from yaga.errors import GateError

REPOSITORY = "dariuszpanas/django-ray"
PULL_REQUEST = 430
HEAD = "a" * 40
BASE = "b" * 40
CANDIDATE = Candidate(
    pull_request_number=PULL_REQUEST,
    head_sha=HEAD,
    base_sha=BASE,
    base_ref="main",
    head_repository="contributor/django-ray",
    head_ref="feat/codex-status",
)
EXPECTED_BASE_PAYLOAD = {
    "base": BASE,
    "base_ref": "main",
    "head": HEAD,
    "head_ref": "feat/codex-status",
    "head_repository": "contributor/django-ray",
    "pr": PULL_REQUEST,
}
OBSERVER_RUN_ID = 32_600_100_001
CONNECTOR_ACTOR = {
    "id": CODEX_CONNECTOR_USER_ID,
    "login": CODEX_CONNECTOR_LOGIN,
}


class NoWriteApi:
    """Resolver API sentinel: candidate selection must remain read-only."""

    def get(self, path: str) -> object:
        raise AssertionError(f"unexpected resolver GET: {path}")

    def paginate(self, path: str) -> list[dict[str, object]]:
        raise AssertionError(f"unexpected resolver pagination: {path}")

    def post(self, path: str, payload: dict[str, object]) -> object:
        raise AssertionError(f"resolver attempted a write: {path} {payload}")


def _pull_request_record() -> dict[str, object]:
    return {
        "number": PULL_REQUEST,
        "head": {
            "sha": HEAD,
            "ref": CANDIDATE.head_ref,
            "repo": {"full_name": CANDIDATE.head_repository},
        },
        "base": {
            "sha": BASE,
            "ref": "main",
            "repo": {"full_name": REPOSITORY},
        },
        "user": {"id": 50_001, "login": "contributor"},
        "draft": False,
        "state": "open",
        "created_at": "2026-08-22T17:00:00Z",
    }


def _observer_run(
    *,
    actor: dict[str, object] | None = None,
    triggering_actor: dict[str, object] | None = None,
    path: str = DEFAULT_OBSERVER_WORKFLOW_PATH,
    head: str = HEAD,
    repository: str = REPOSITORY,
) -> dict[str, object]:
    return {
        "id": OBSERVER_RUN_ID,
        "path": path,
        "event": "pull_request_review",
        "status": "completed",
        "conclusion": "success",
        "head_sha": head,
        "repository": {"full_name": repository},
        "actor": copy.deepcopy(actor or CONNECTOR_ACTOR),
        "triggering_actor": copy.deepcopy(triggering_actor or CONNECTOR_ACTOR),
    }


def _workflow_run_event(
    *,
    run: dict[str, object] | None = None,
    repository: str = REPOSITORY,
) -> dict[str, object]:
    return {
        "repository": {"full_name": repository},
        "workflow_run": copy.deepcopy(run or _observer_run()),
    }


class ObserverApi(NoWriteApi):
    def __init__(
        self,
        *,
        source: dict[str, object] | None = None,
        associations: list[dict[str, object]] | None = None,
    ) -> None:
        self.source = source or _observer_run()
        self.associations = (
            [{"number": PULL_REQUEST, "state": "open", "head": {"sha": HEAD}}]
            if associations is None
            else associations
        )

    def get(self, path: str) -> object:
        if path == f"/repos/{REPOSITORY}/actions/runs/{OBSERVER_RUN_ID}":
            return copy.deepcopy(self.source)
        if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}":
            return _pull_request_record()
        if path == f"/repos/{REPOSITORY}":
            return {"full_name": REPOSITORY, "default_branch": "main"}
        return super().get(path)

    def paginate(self, path: str) -> list[dict[str, object]]:
        if path == f"/repos/{REPOSITORY}/commits/{HEAD}/pulls":
            return copy.deepcopy(self.associations)
        return super().paginate(path)


def _comment_event(
    *,
    body: object = "Codex Review: outcome",
    user_id: int = CODEX_CONNECTOR_USER_ID,
    login: str = CODEX_CONNECTOR_LOGIN,
    association: str = "NONE",
    issue_state: str = "open",
    pull_request: object = ...,
    number: object = PULL_REQUEST,
) -> dict[str, object]:
    if pull_request is ...:
        pull_request = {"url": f"https://api.github.com/repos/{REPOSITORY}/pulls/{PULL_REQUEST}"}
    return {
        "issue": {
            "number": number,
            "pull_request": pull_request,
            "state": issue_state,
        },
        "comment": {
            "author_association": association,
            "body": body,
            "user": {"id": user_id, "login": login},
        },
    }


def _stub_candidates(
    monkeypatch: pytest.MonkeyPatch,
    candidates: list[Candidate],
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_wake_candidate(
        _api: object,
        **kwargs: object,
    ) -> Candidate | None:
        calls.append(kwargs)
        return candidates[0] if candidates else None

    monkeypatch.setattr(resolver, "wake_candidate", fake_wake_candidate)
    return calls


def test_schedule_requires_the_separate_write_capable_repair_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_candidates(monkeypatch, [CANDIDATE])

    with pytest.raises(GateError, match="write-capable boundary repair"):
        resolver.resolve_event_candidates(
            NoWriteApi(),
            repository=REPOSITORY,
            event_name="schedule",
            event={},
        )
    assert calls == []


def test_exact_connector_observer_run_wakes_its_live_head_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_candidates(monkeypatch, [CANDIDATE])

    result = resolver.resolve_event_candidates(
        ObserverApi(),
        repository=REPOSITORY,
        event_name="workflow_run",
        event=_workflow_run_event(),
    )

    assert calls == [
        {
            "repository": REPOSITORY,
            "pull_request_number": PULL_REQUEST,
        }
    ]
    assert result == [EXPECTED_BASE_PAYLOAD]


@pytest.mark.parametrize(
    ("event", "source"),
    [
        (
            _workflow_run_event(
                run=_observer_run(
                    actor={
                        "id": CODEX_CONNECTOR_USER_ID + 1,
                        "login": CODEX_CONNECTOR_LOGIN,
                    }
                )
            ),
            _observer_run(),
        ),
        (
            _workflow_run_event(run=_observer_run(path=".github/workflows/untrusted.yml")),
            _observer_run(),
        ),
        (
            _workflow_run_event(),
            _observer_run(triggering_actor={"id": 50_001, "login": "maintainer"}),
        ),
        (
            _workflow_run_event(),
            _observer_run(head="c" * 40),
        ),
    ],
)
def test_workflow_run_requires_exact_observer_and_connector_identity(
    monkeypatch: pytest.MonkeyPatch,
    event: dict[str, object],
    source: dict[str, object],
) -> None:
    calls = _stub_candidates(monkeypatch, [CANDIDATE])
    with pytest.raises(GateError):
        resolver.resolve_event_candidates(
            ObserverApi(source=source),
            repository=REPOSITORY,
            event_name="workflow_run",
            event=event,
        )
    assert calls == []


def test_workflow_run_without_a_unique_open_head_owner_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_candidates(monkeypatch, [CANDIDATE])
    assert (
        resolver.resolve_event_candidates(
            ObserverApi(associations=[]),
            repository=REPOSITORY,
            event_name="workflow_run",
            event=_workflow_run_event(),
        )
        == []
    )
    assert calls == []


def test_exact_connector_comment_wakes_only_its_pull_request_for_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_candidates(monkeypatch, [CANDIDATE])

    result = resolver.resolve_event_candidates(
        NoWriteApi(),
        repository=REPOSITORY,
        event_name="issue_comment",
        event=_comment_event(body="any connector-authored outcome body"),
    )

    assert calls == [
        {
            "repository": REPOSITORY,
            "pull_request_number": PULL_REQUEST,
        }
    ]
    assert result == [EXPECTED_BASE_PAYLOAD]


def test_owner_review_request_wakes_only_its_candidate_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_candidates(monkeypatch, [CANDIDATE])

    result = resolver.resolve_event_candidates(
        NoWriteApi(),
        repository=REPOSITORY,
        event_name="issue_comment",
        event=_comment_event(
            body="@codex review",
            user_id=15_094_983,
            login="dariuszpanas",
            association="OWNER",
        ),
    )

    assert len(calls) == 1
    assert calls[0]["pull_request_number"] == PULL_REQUEST
    assert result == [EXPECTED_BASE_PAYLOAD]
    assert Candidate.from_json(json.dumps(result[0])) == CANDIDATE


@pytest.mark.parametrize(
    ("event_name", "event"),
    [
        ("workflow_run", _workflow_run_event()),
        ("issue_comment", _comment_event(body="connector outcome")),
        (
            "issue_comment",
            _comment_event(
                body="@codex review",
                user_id=15_094_983,
                login="dariuszpanas",
                association="OWNER",
            ),
        ),
    ],
)
def test_trusted_wakes_emit_the_live_candidate_without_reading_status(
    event_name: str,
    event: dict[str, object],
) -> None:
    api = ObserverApi()

    assert resolver.resolve_event_candidates(
        api,
        repository=REPOSITORY,
        event_name=event_name,
        event=event,
    ) == [EXPECTED_BASE_PAYLOAD]


@pytest.mark.parametrize(
    "event",
    [
        _comment_event(user_id=CODEX_CONNECTOR_USER_ID + 1),
        _comment_event(login="codex-lookalike[bot]"),
        _comment_event(
            body="@codex review",
            user_id=50_001,
            login="member",
            association="MEMBER",
        ),
        _comment_event(issue_state="closed"),
        _comment_event(pull_request=None),
        _comment_event(
            body="ordinary owner comment",
            user_id=15_094_983,
            login="dariuszpanas",
            association="OWNER",
        ),
    ],
)
def test_irrelevant_or_spoofed_comment_does_not_scan_candidates(
    monkeypatch: pytest.MonkeyPatch,
    event: dict[str, object],
) -> None:
    calls = _stub_candidates(monkeypatch, [CANDIDATE])
    assert (
        resolver.resolve_event_candidates(
            NoWriteApi(),
            repository=REPOSITORY,
            event_name="issue_comment",
            event=event,
        )
        == []
    )
    assert calls == []


@pytest.mark.parametrize(
    "body",
    [
        "@codex reviewer",
        "@codex reviews",
        "@codex review-this",
        " @codex review",
        "@codex review ",
        "@codex review please",
        "@codex review\nPlease check the current head.",
    ],
)
def test_owner_command_requires_a_review_token_boundary(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> None:
    calls = _stub_candidates(monkeypatch, [CANDIDATE])
    event = _comment_event(
        body=body,
        user_id=15_094_983,
        login="dariuszpanas",
        association="OWNER",
    )

    assert (
        resolver.resolve_event_candidates(
            NoWriteApi(),
            repository=REPOSITORY,
            event_name="issue_comment",
            event=event,
        )
        == []
    )
    assert calls == []


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"issue": [], "comment": {}},
        {"issue": {}, "comment": []},
        _comment_event(number=0),
        _comment_event(number=True),
    ],
)
def test_malformed_comment_event_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    event: dict[str, object],
) -> None:
    _stub_candidates(monkeypatch, [CANDIDATE])
    with pytest.raises(GateError):
        resolver.resolve_event_candidates(
            NoWriteApi(),
            repository=REPOSITORY,
            event_name="issue_comment",
            event=event,
        )


def test_unsupported_event_fails_closed_without_candidate_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_candidates(monkeypatch, [CANDIDATE])
    with pytest.raises(GateError, match="unsupported"):
        resolver.resolve_event_candidates(
            NoWriteApi(),
            repository=REPOSITORY,
            event_name="workflow_dispatch",
            event={},
        )
    assert calls == []


def test_empty_candidate_set_emits_no_matrix_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_candidates(monkeypatch, [])
    assert (
        resolver.resolve_event_candidates(
            NoWriteApi(),
            repository=REPOSITORY,
            event_name="issue_comment",
            event=_comment_event(),
        )
        == []
    )


def test_all_candidate_payloads_are_accepted_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_candidates(monkeypatch, [CANDIDATE])
    for event_name, event in (
        ("issue_comment", _comment_event()),
        (
            "issue_comment",
            _comment_event(
                body="@codex review",
                user_id=15_094_983,
                login="dariuszpanas",
                association="OWNER",
            ),
        ),
    ):
        payload = resolver.resolve_event_candidates(
            NoWriteApi(),
            repository=REPOSITORY,
            event_name=event_name,
            event=event,
        )[0]
        assert Candidate.from_json(json.dumps(payload)) == CANDIDATE
