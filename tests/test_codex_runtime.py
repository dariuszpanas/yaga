"""Tests for operation dispatch and closed composite outputs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.codex_support import (
    BASE_REF,
    CI_WORKFLOW_PATH,
    PULL_REQUEST,
    REPOSITORY,
    RUN_ATTEMPT,
    RUN_ID,
    _event,
)
from yaga.codex import runtime
from yaga.codex.constants import (
    MAX_AUTHORIZATION_REQUESTS,
    MAX_FINALIZATION_REQUESTS,
    MAX_INVALIDATION_REQUESTS,
    MAX_POLL_REQUESTS,
    MAX_PREPARATION_REQUESTS,
)
from yaga.codex.gate import GateResult
from yaga.errors import GateError

WORKFLOW_PATH = ".github/workflows/agent-review.yml"


def _environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    event_name: str,
    event: dict[str, object],
) -> Path:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    output_path = tmp_path / "output.txt"
    values = {
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_OUTPUT": str(output_path),
        "GITHUB_REF": f"refs/heads/{BASE_REF}",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_RUN_ATTEMPT": str(RUN_ATTEMPT),
        "GITHUB_RUN_ID": str(RUN_ID),
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_TOKEN": "secret-token",
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/{WORKFLOW_PATH}@refs/heads/{BASE_REF}",
        "YAGA_JOB_TIMEOUT_MINUTES": "15",
        "YAGA_APPROVAL_MARKER": runtime.APPROVAL_ENVIRONMENT_MARKER,
        "YAGA_LIFECYCLE_WORKFLOW": ".github/workflows/review-policy.yml",
        "YAGA_OWNER_ID": "15094983",
        "YAGA_PREREQUISITE_WORKFLOW": ".github/workflows/ci.yml",
        "YAGA_REQUEST_TIMEOUT": "15",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return output_path


def test_operation_name_is_closed() -> None:
    assert runtime.operation_name("prepare") == "prepare"
    assert runtime.operation_name("authorize") == "authorize"
    with pytest.raises(GateError, match="operation"):
        runtime.operation_name("publish")


def test_invalidate_dispatches_without_loading_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = _environment(
        monkeypatch,
        tmp_path,
        event_name="pull_request_target",
        event=_event(),
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(runtime, "GitHubRestApi", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        runtime,
        "load_source_from_wake",
        lambda *_args, **_kwargs: pytest.fail("invalidate loaded a CI source"),
    )
    monkeypatch.setenv("YAGA_OPERATION", "prepare")

    def fake_invalidate(_api: object, **kwargs: object) -> GateResult:
        captured.update(kwargs)
        return GateResult("pending", 0)

    monkeypatch.setattr(runtime, "invalidate", fake_invalidate)
    assert runtime.run_action("invalidate") == 0
    assert captured["run_id"] == RUN_ID
    assert captured["run_attempt"] == RUN_ATTEMPT
    assert output.read_text(encoding="utf-8") == "route=skip\n"


def test_prepare_dispatches_owner_id_and_publishes_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    event: dict[str, object] = {
        "action": "completed",
        "repository": {"full_name": REPOSITORY, "default_branch": BASE_REF},
        "workflow_run": {"path": CI_WORKFLOW_PATH},
    }
    output = _environment(
        monkeypatch,
        tmp_path,
        event_name="workflow_run",
        event=event,
    )
    api = object()
    source = SimpleNamespace(pull_request_number=PULL_REQUEST)
    captured: dict[str, object] = {}
    monkeypatch.setattr(runtime, "GitHubRestApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(runtime, "load_source_from_wake", lambda *_args, **_kwargs: source)

    def fake_prepare(_api: object, **kwargs: object) -> GateResult:
        captured.update(kwargs)
        return GateResult("route", 0, "external")

    monkeypatch.setattr(runtime, "prepare", fake_prepare)
    assert runtime.run_action("prepare") == 0
    assert captured["source"] is source
    assert captured["wake_workflow"] == CI_WORKFLOW_PATH
    assert captured["direct_author_id"] == 15_094_983
    assert output.read_text(encoding="utf-8") == (
        f"route=external\npull_request_number={PULL_REQUEST}\n"
    )


def test_authorize_requires_the_protected_environment_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    event: dict[str, object] = {
        "action": "completed",
        "repository": {"full_name": REPOSITORY, "default_branch": BASE_REF},
        "workflow_run": {},
    }
    _environment(
        monkeypatch,
        tmp_path,
        event_name="workflow_run",
        event=event,
    )
    monkeypatch.setenv("YAGA_APPROVAL_MARKER", "")
    monkeypatch.setattr(runtime, "GitHubRestApi", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        runtime,
        "load_source_from_wake",
        lambda *_args, **_kwargs: SimpleNamespace(pull_request_number=PULL_REQUEST),
    )
    monkeypatch.setattr(
        runtime,
        "authorize",
        lambda *_args, **_kwargs: pytest.fail("unprotected authorize was dispatched"),
    )

    with pytest.raises(GateError, match="YAGA_APPROVAL_MARKER"):
        runtime.run_action("authorize")


@pytest.mark.parametrize(("operation", "allow_request"), [("observe", False), ("request", True)])
def test_review_modes_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    allow_request: bool,
) -> None:
    event: dict[str, object] = {
        "action": "completed",
        "repository": {"full_name": REPOSITORY, "default_branch": BASE_REF},
        "workflow_run": {},
    }
    _environment(
        monkeypatch,
        tmp_path,
        event_name="workflow_run",
        event=event,
    )
    monkeypatch.setattr(runtime, "GitHubRestApi", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "load_source_from_wake", lambda *_args, **_kwargs: object())

    def fake_review(_api: object, **kwargs: object) -> GateResult:
        assert kwargs["allow_request"] is allow_request
        return GateResult("done", 0, "done")

    monkeypatch.setattr(runtime, "review", fake_review)
    assert runtime.run_action(operation) == 0


def test_default_branch_workflow_ref_is_mandatory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _environment(
        monkeypatch,
        tmp_path,
        event_name="pull_request_target",
        event=_event(),
    )
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", f"{REPOSITORY}/{WORKFLOW_PATH}@refs/heads/feature")
    with pytest.raises(GateError, match="default branch"):
        runtime.run_action("invalidate")


@pytest.mark.parametrize(
    ("operation", "expected_budget"),
    [
        ("invalidate", MAX_INVALIDATION_REQUESTS),
        ("authorize", MAX_AUTHORIZATION_REQUESTS),
        ("prepare", MAX_PREPARATION_REQUESTS),
        ("observe", MAX_POLL_REQUESTS),
        ("request", MAX_POLL_REQUESTS),
        ("finalize", MAX_FINALIZATION_REQUESTS),
    ],
)
def test_each_operation_has_a_closed_api_request_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    expected_budget: int,
) -> None:
    event_name = "pull_request_target" if operation == "invalidate" else "workflow_run"
    event = (
        _event()
        if operation == "invalidate"
        else {
            "action": "completed",
            "repository": {"full_name": REPOSITORY, "default_branch": BASE_REF},
            "workflow_run": {"path": CI_WORKFLOW_PATH},
        }
    )
    _environment(
        monkeypatch,
        tmp_path,
        event_name=event_name,
        event=event,
    )
    captured: dict[str, object] = {}

    def fake_api(*_args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(runtime, "GitHubRestApi", fake_api)
    monkeypatch.setattr(
        runtime,
        "load_source_from_wake",
        lambda *_args, **_kwargs: SimpleNamespace(pull_request_number=PULL_REQUEST),
    )
    monkeypatch.setattr(runtime, "invalidate", lambda *_args, **_kwargs: GateResult("done", 0))
    monkeypatch.setattr(runtime, "prepare", lambda *_args, **_kwargs: GateResult("done", 0))
    monkeypatch.setattr(runtime, "authorize", lambda *_args, **_kwargs: GateResult("done", 0))
    monkeypatch.setattr(runtime, "review", lambda *_args, **_kwargs: GateResult("done", 0))
    monkeypatch.setattr(runtime, "finalize", lambda *_args, **_kwargs: GateResult("done", 0))

    assert runtime.run_action(operation) == 0
    assert captured["max_requests"] == expected_budget
