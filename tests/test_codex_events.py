"""Tests for the trusted direct pull-request event boundary."""

from __future__ import annotations

import pytest

from tests.codex_support import BASE_REF, PULL_REQUEST, REPOSITORY, _event, _pull_request
from yaga.codex.events import CodexEvent, parse_codex_event, parse_event_boundary
from yaga.errors import GateError


def _parse(event: dict[str, object]) -> CodexEvent:
    return parse_codex_event(
        repository=REPOSITORY,
        event_name="pull_request_target",
        event=event,
    )


@pytest.mark.parametrize(
    ("action", "disposition"),
    [
        ("opened", "review"),
        ("synchronize", "review"),
        ("reopened", "review"),
        ("ready_for_review", "review"),
    ],
)
def test_ready_boundaries_enter_review_policy(action: str, disposition: str) -> None:
    parsed = _parse(_event(action=action))

    assert parsed.disposition == disposition
    assert parsed.default_branch == BASE_REF
    assert parsed.pull_request_number == PULL_REQUEST


def test_ordinary_edit_is_noop_but_base_edit_is_a_review_boundary() -> None:
    assert _parse(_event(action="edited")).disposition == "noop"
    assert _parse(_event(action="edited", base_changed=True)).disposition == "review"


def test_every_draft_boundary_persists_pending() -> None:
    draft = _pull_request(draft=True)

    assert _parse(_event(action="opened", pull_request=draft)).disposition == "draft"
    for action in ("converted_to_draft", "reopened", "synchronize"):
        assert _parse(_event(action=action, pull_request=draft)).disposition == "draft"


def test_ready_transition_must_actually_be_ready() -> None:
    with pytest.raises(GateError, match="still identifies a draft"):
        _parse(_event(action="ready_for_review", pull_request=_pull_request(draft=True)))


def test_closed_deleted_fork_is_a_defensive_noop_without_parsing_head() -> None:
    closed = _pull_request(state="closed")
    head = closed["head"]
    assert isinstance(head, dict)
    head["repo"] = None

    parsed = _parse(_event(action="closed", pull_request=closed))

    assert parsed.disposition == "closed"
    assert parsed.pull_request is None


def test_active_deleted_fork_can_publish_pending_before_full_parse_fails() -> None:
    event = _event()
    pull_request = event["pull_request"]
    assert isinstance(pull_request, dict)
    head = pull_request["head"]
    assert isinstance(head, dict)
    head["repo"] = None

    boundary = parse_event_boundary(
        repository=REPOSITORY,
        event_name="pull_request_target",
        event=event,
    )
    assert boundary.head_sha is not None
    with pytest.raises(GateError, match="head repository"):
        _parse(event)


def test_event_rejects_cross_repository_and_number_mismatch() -> None:
    wrong_repository = _event()
    wrong_repository["repository"] = {
        "default_branch": BASE_REF,
        "full_name": "attacker/repository",
    }
    with pytest.raises(GateError, match="another repository"):
        _parse(wrong_repository)

    wrong_number = _event()
    wrong_number["number"] = PULL_REQUEST + 1
    with pytest.raises(GateError, match="different pull request"):
        _parse(wrong_number)


@pytest.mark.parametrize("event_name", ["pull_request", "workflow_run", "schedule"])
def test_event_requires_pull_request_target(event_name: str) -> None:
    with pytest.raises(GateError, match="requires a pull_request_target"):
        parse_codex_event(
            repository=REPOSITORY,
            event_name=event_name,
            event=_event(),
        )
