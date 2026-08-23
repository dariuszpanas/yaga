"""Composite-action runtime for the authoritative Codex review gate."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from yaga.codex import publisher, resolver, scheduler
from yaga.codex.constants import (
    MAX_SCHEDULE_RECONCILE_REQUESTS,
    MAX_TERMINAL_JOB_TIMEOUT_MINUTES,
    MIN_TERMINAL_JOB_TIMEOUT_MINUTES,
)
from yaga.codex.models import Candidate
from yaga.errors import GateError
from yaga.github import MAX_API_REQUESTS, GitHubRestApi
from yaga.models import (
    bounded_text,
    positive_int,
    read_json_object,
    record,
    ref_name,
    repository_name,
    request_timeout,
    workflow_path,
)

MAX_OUTPUT_BYTES = 512 * 1024
TERMINAL_MODES = frozenset(
    {"reconcile-boundary", "reconcile-candidate", "reconcile-repair-candidate"}
)


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise GateError(f"required GitHub environment is missing: {name}")
    return value


def _validate_workflow_context(repository: str, event: dict[str, object]) -> str:
    observer = workflow_path(
        _required_environment("YAGA_OBSERVER_WORKFLOW_PATH"),
        "Codex review observer workflow path",
    )
    event_repository = record(event.get("repository"), "GitHub event repository")
    if event_repository.get("full_name") != repository:
        raise GateError("GitHub event belongs to another repository")
    default_branch = ref_name(event_repository.get("default_branch"), "default branch")
    workflow_ref = bounded_text(
        _required_environment("GITHUB_WORKFLOW_REF"),
        "GitHub workflow ref",
        max_bytes=512,
    )
    prefix = f"{repository}/"
    suffix = f"@refs/heads/{default_branch}"
    if not workflow_ref.startswith(prefix) or not workflow_ref.endswith(suffix):
        raise GateError("the publisher is not running from the default branch")
    publisher_path = workflow_path(
        workflow_ref[len(prefix) : -len(suffix)],
        "Codex review publisher workflow path",
    )
    if publisher_path == observer:
        raise GateError("observer and publisher workflows must be separate")
    if _required_environment("GITHUB_REF") != f"refs/heads/{default_branch}":
        raise GateError("the publisher ref is not the default branch")
    return observer


def _write_outputs(**values: str) -> None:
    output_path = _required_environment("GITHUB_OUTPUT")
    payload = "".join(f"{name}={value}\n" for name, value in values.items())
    if len(payload.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise GateError("YAGA outputs exceeded the byte limit")
    try:
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as output:
            output.write(payload)
    except OSError as error:
        raise GateError("GITHUB_OUTPUT could not be written") from error


def _common(
    *,
    max_requests: int,
    success_deadline: float | None,
) -> tuple[GitHubRestApi, str, str, str, dict[str, object]]:
    repository = repository_name(_required_environment("GITHUB_REPOSITORY"))
    event_name = bounded_text(
        _required_environment("GITHUB_EVENT_NAME"),
        "GitHub event name",
        max_bytes=64,
    )
    event = read_json_object(
        _required_environment("GITHUB_EVENT_PATH"),
        label="GitHub event payload",
    )
    _validate_workflow_context(repository, event)
    server_url = bounded_text(
        _required_environment("GITHUB_SERVER_URL"),
        "GitHub server URL",
        max_bytes=2_048,
    )
    timeout = request_timeout(_required_environment("YAGA_REQUEST_TIMEOUT"))
    api = GitHubRestApi(
        _required_environment("GITHUB_TOKEN"),
        base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        timeout=timeout,
        max_requests=max_requests,
        success_deadline=success_deadline,
    )
    return api, repository, server_url, event_name, event


def _candidate_input() -> Candidate:
    return Candidate.from_json(_required_environment("YAGA_CANDIDATE"))


def _environment_positive_int(name: str, label: str) -> int:
    value = _required_environment(name)
    if not value.isascii() or not value.isdigit():
        raise GateError(f"{label} must be a bounded positive integer")
    return positive_int(int(value), label)


def _success_deadline(mode: str, *, started_at: float) -> float | None:
    """Return the declared terminal-job deadline used for success admission."""
    if mode not in TERMINAL_MODES:
        return None
    value = os.environ.get("YAGA_JOB_TIMEOUT_MINUTES", "")
    if not value.isascii() or not value.isdigit():
        raise GateError("terminal job timeout must be a bounded positive integer")
    minutes = positive_int(int(value), "terminal job timeout")
    if not MIN_TERMINAL_JOB_TIMEOUT_MINUTES <= minutes <= MAX_TERMINAL_JOB_TIMEOUT_MINUTES:
        raise GateError(
            "terminal job timeout must be between "
            f"{MIN_TERMINAL_JOB_TIMEOUT_MINUTES} and "
            f"{MAX_TERMINAL_JOB_TIMEOUT_MINUTES} minutes"
        )
    return started_at + minutes * 60


def run_action(mode: str) -> int:
    """Execute one closed orchestration mode and write compact action outputs."""
    started_at = time.monotonic()
    request_budget = (
        MAX_SCHEDULE_RECONCILE_REQUESTS
        if mode == "reconcile-repair-candidate"
        else MAX_API_REQUESTS
    )
    api, repository, server_url, event_name, event = _common(
        max_requests=request_budget,
        success_deadline=_success_deadline(mode, started_at=started_at),
    )
    candidate_output = ""
    candidates_output = "[]"
    eligible = False

    if mode == "invalidate-boundary":
        candidate = publisher.invalidate_lifecycle_event(
            api,
            repository=repository,
            server_url=server_url,
            event_name=event_name,
            event=event,
        )
        if candidate is not None:
            candidate_output = candidate.to_json()
            eligible = True
    elif mode == "reconcile-boundary":
        candidate = _candidate_input()
        result = publisher.reconcile_lifecycle_event(
            api,
            repository=repository,
            server_url=server_url,
            event_name=event_name,
            event=event,
            expected_candidate=candidate,
            reconciliation_run_id=_environment_positive_int(
                "GITHUB_RUN_ID", "GitHub workflow run ID"
            ),
        )
        print(f"Codex review publication: {result}")
    elif mode == "resolve":
        candidates = resolver.resolve_event_candidates(
            api,
            repository=repository,
            event_name=event_name,
            event=event,
        )
        candidates_output = json.dumps(candidates, separators=(",", ":"))
        eligible = bool(candidates)
    elif mode == "repair-boundaries":
        if event_name != "schedule":
            raise GateError("scheduled boundary repair requires a schedule event")
        candidates = scheduler.repair_scheduled_boundaries(
            api,
            repository=repository,
            server_url=server_url,
            repair_run_id=_environment_positive_int("GITHUB_RUN_ID", "GitHub workflow run ID"),
        )
        candidates_output = json.dumps(
            [candidate.to_payload() for candidate in candidates],
            separators=(",", ":"),
        )
        eligible = bool(candidates)
    elif mode in {"reconcile-candidate", "reconcile-repair-candidate"}:
        if mode == "reconcile-repair-candidate" and event_name != "schedule":
            raise GateError("scheduled candidate reconciliation requires a schedule event")
        candidate = _candidate_input()
        result = publisher.reconcile_candidate(
            api,
            repository=repository,
            server_url=server_url,
            candidate=candidate,
            reconciliation_run_id=_environment_positive_int(
                "GITHUB_RUN_ID", "GitHub workflow run ID"
            ),
        )
        print(f"Codex review publication: {result}")
    else:
        raise GateError("action mode is invalid")

    _write_outputs(
        candidate=candidate_output,
        candidates=candidates_output,
        eligible="true" if eligible else "false",
    )
    return 0
