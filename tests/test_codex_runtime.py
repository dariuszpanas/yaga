"""Tests for the six composite-action orchestration modes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from yaga.codex import runtime
from yaga.codex.models import Candidate
from yaga.errors import GateError

REPOSITORY = "owner/repository"
CANDIDATE = Candidate(
    pull_request_number=7,
    head_sha="a" * 40,
    base_sha="b" * 40,
    base_ref="main",
    head_repository="contributor/repository",
    head_ref="feat/yaga",
)


def _environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    event_name: str = "workflow_run",
) -> tuple[dict[str, object], Path]:
    event = {
        "repository": {
            "default_branch": "main",
            "full_name": REPOSITORY,
        },
        "workflow_run": {"id": 99},
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    output_path = tmp_path / "output.txt"
    values = {
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_OUTPUT": str(output_path),
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_RUN_ID": "100",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_TOKEN": "test-token",
        "GITHUB_WORKFLOW_REF": (f"{REPOSITORY}/.github/workflows/codex-review.yml@refs/heads/main"),
        "YAGA_CANDIDATE": CANDIDATE.to_json(),
        "YAGA_JOB_TIMEOUT_MINUTES": "15",
        "YAGA_OBSERVER_WORKFLOW_PATH": ".github/workflows/review-policy-event.yml",
        "YAGA_REQUEST_TIMEOUT": "15",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(runtime, "GitHubRestApi", lambda *_args, **_kwargs: object())
    return event, output_path


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())


def test_invalidate_boundary_emits_one_exact_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    event, output_path = _environment(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def invalidate(_api: object, **kwargs: Any) -> Candidate:
        captured.update(kwargs)
        return CANDIDATE

    monkeypatch.setattr(runtime.publisher, "invalidate_lifecycle_event", invalidate)

    assert runtime.run_action("invalidate-boundary") == 0
    assert captured["event"] == event
    assert captured["event_name"] == "workflow_run"
    assert _outputs(output_path) == {
        "candidate": CANDIDATE.to_json(),
        "candidates": "[]",
        "eligible": "true",
    }


def test_irrelevant_boundary_emits_empty_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, output_path = _environment(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime.publisher,
        "invalidate_lifecycle_event",
        lambda *_args, **_kwargs: None,
    )

    assert runtime.run_action("invalidate-boundary") == 0
    assert _outputs(output_path) == {
        "candidate": "",
        "candidates": "[]",
        "eligible": "false",
    }


def test_reconcile_boundary_revalidates_the_handoff_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, output_path = _environment(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def reconcile(_api: object, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "success"

    monkeypatch.setattr(runtime.publisher, "reconcile_lifecycle_event", reconcile)

    assert runtime.run_action("reconcile-boundary") == 0
    assert captured["expected_candidate"] == CANDIDATE
    assert _outputs(output_path)["eligible"] == "false"


def test_resolve_emits_a_compact_bounded_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, output_path = _environment(monkeypatch, tmp_path, event_name="issue_comment")
    monkeypatch.setattr(
        runtime.resolver,
        "resolve_event_candidates",
        lambda *_args, **_kwargs: [CANDIDATE.to_payload()],
    )

    assert runtime.run_action("resolve") == 0
    outputs = _outputs(output_path)
    assert outputs["eligible"] == "true"
    assert json.loads(outputs["candidates"]) == [CANDIDATE.to_payload()]


def test_reconcile_candidate_uses_the_public_adapter_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, output_path = _environment(monkeypatch, tmp_path, event_name="issue_comment")
    captured: dict[str, Any] = {}

    def reconcile(_api: object, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "pending"

    monkeypatch.setattr(runtime.publisher, "reconcile_candidate", reconcile)

    assert runtime.run_action("reconcile-candidate") == 0
    assert captured["candidate"] == CANDIDATE
    assert _outputs(output_path)["eligible"] == "false"


def test_scheduled_repair_emits_only_its_bounded_terminal_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, output_path = _environment(monkeypatch, tmp_path, event_name="schedule")
    captured: dict[str, Any] = {}

    def repair(_api: object, **kwargs: Any) -> list[Candidate]:
        captured.update(kwargs)
        return [CANDIDATE]

    monkeypatch.setattr(runtime.scheduler, "repair_scheduled_boundaries", repair)

    assert runtime.run_action("repair-boundaries") == 0
    outputs = _outputs(output_path)
    assert outputs["eligible"] == "true"
    assert json.loads(outputs["candidates"]) == [CANDIDATE.to_payload()]
    assert captured["repair_run_id"] == 100


def test_scheduled_repair_rejects_a_malformed_workflow_run_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _environment(monkeypatch, tmp_path, event_name="schedule")
    monkeypatch.setenv("GITHUB_RUN_ID", "+100")

    with pytest.raises(GateError, match="workflow run ID"):
        runtime.run_action("repair-boundaries")


def test_scheduled_terminal_writer_uses_the_smaller_fixed_request_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _environment(monkeypatch, tmp_path, event_name="schedule")
    budgets: list[int] = []
    deadlines: list[float | None] = []
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 100.0)

    def fake_api(
        *_args: object,
        max_requests: int,
        success_deadline: float | None,
        **_kwargs: object,
    ) -> object:
        budgets.append(max_requests)
        deadlines.append(success_deadline)
        return object()

    monkeypatch.setattr(runtime, "GitHubRestApi", fake_api)
    monkeypatch.setattr(
        runtime.publisher,
        "reconcile_candidate",
        lambda *_args, **_kwargs: "pending",
    )

    assert runtime.run_action("reconcile-repair-candidate") == 0
    assert budgets == [runtime.MAX_SCHEDULE_RECONCILE_REQUESTS]
    assert deadlines == [1_000.0]


@pytest.mark.parametrize("value", ["", "14", "361", "15.5"])
def test_terminal_writer_rejects_a_missing_or_unsafe_job_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    value: str,
) -> None:
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv("YAGA_JOB_TIMEOUT_MINUTES", value)

    with pytest.raises(GateError, match="terminal job timeout"):
        runtime.run_action("reconcile-candidate")


def test_runtime_requires_the_default_branch_publisher_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/feature")

    with pytest.raises(GateError, match="default branch"):
        runtime.run_action("resolve")


def test_runtime_keeps_observer_and_publisher_workflows_separate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        f"{REPOSITORY}/.github/workflows/review-policy-event.yml@refs/heads/main",
    )

    with pytest.raises(GateError, match="must be separate"):
        runtime.run_action("resolve")


def test_runtime_rejects_unknown_modes_even_when_called_directly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _environment(monkeypatch, tmp_path)

    with pytest.raises(GateError, match="action mode"):
        runtime.run_action("restore")
