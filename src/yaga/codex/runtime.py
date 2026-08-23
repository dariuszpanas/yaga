"""Composite-action runtime for YAGA's split trusted operations."""

from __future__ import annotations

import os
import time
import urllib.parse
from pathlib import Path

from yaga.codex.constants import (
    LIFECYCLE_WAIT_SECONDS,
    MAX_AUTHORIZATION_REQUESTS,
    MAX_FINALIZATION_REQUESTS,
    MAX_INVALIDATION_REQUESTS,
    MAX_JOB_TIMEOUT_MINUTES,
    MAX_POLL_REQUESTS,
    MAX_PREPARATION_REQUESTS,
    MIN_JOB_TIMEOUT_MINUTES,
    POLL_WINDOW_SECONDS,
    SUCCESS_CLEANUP_MARGIN_SECONDS,
    SUCCESS_WRITE_REQUEST_RESERVE,
)
from yaga.codex.events import parse_event_boundary
from yaga.codex.gate import authorize, finalize, invalidate, prepare, review
from yaga.codex.runs import load_source_from_wake
from yaga.errors import GateError
from yaga.github import MAX_API_REQUESTS, GitHubRestApi
from yaga.models import (
    bounded_text,
    positive_int,
    read_json_object,
    record,
    repository_name,
    request_timeout,
    workflow_path,
)

OPERATIONS = frozenset({"authorize", "finalize", "invalidate", "observe", "prepare", "request"})
APPROVAL_ENVIRONMENT_MARKER = "codex-review-approval:v1"


def operation_name(value: object) -> str:
    """Accept only a deliberately implemented action operation."""
    if not isinstance(value, str) or value not in OPERATIONS:
        raise GateError("operation is invalid")
    return value


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise GateError(f"required GitHub environment is missing: {name}")
    return value


def _environment_positive_int(name: str, label: str) -> int:
    value = _required_environment(name)
    if not value.isascii() or not value.isdigit():
        raise GateError(f"{label} must be a bounded positive integer")
    return positive_int(int(value), label)


def _job_timeout_minutes() -> int:
    value = _required_environment("YAGA_JOB_TIMEOUT_MINUTES")
    if not value.isascii() or not value.isdigit():
        raise GateError("YAGA job timeout must be a bounded positive integer")
    minutes = positive_int(int(value), "YAGA job timeout")
    if not MIN_JOB_TIMEOUT_MINUTES <= minutes <= MAX_JOB_TIMEOUT_MINUTES:
        raise GateError(
            "YAGA job timeout must be between "
            f"{MIN_JOB_TIMEOUT_MINUTES} and {MAX_JOB_TIMEOUT_MINUTES} minutes"
        )
    return minutes


def _server_url() -> str:
    value = bounded_text(
        _required_environment("GITHUB_SERVER_URL"),
        "GitHub server URL",
        max_bytes=2_048,
    )
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise GateError("GitHub server URL is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65_535)
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise GateError("GitHub server URL is invalid")
    return value.rstrip("/")


def _attempt_url(server_url: str, repository: str, run_id: int, attempt: int) -> str:
    return f"{server_url}/{repository}/actions/runs/{run_id}/attempts/{attempt}"


def _default_branch(event: dict[str, object]) -> str:
    repository = event.get("repository")
    if not isinstance(repository, dict):
        raise GateError("GitHub event repository is invalid")
    value = repository.get("default_branch")
    return bounded_text(value, "default branch", max_bytes=255)


def _trusted_workflow_path(repository: str, default_branch: str) -> str:
    workflow_ref = bounded_text(
        _required_environment("GITHUB_WORKFLOW_REF"),
        "GitHub workflow ref",
        max_bytes=512,
    )
    prefix = f"{repository}/"
    suffix = f"@refs/heads/{default_branch}"
    if not workflow_ref.startswith(prefix) or not workflow_ref.endswith(suffix):
        raise GateError("YAGA is not running from the repository default branch")
    workflow = workflow_path(workflow_ref[len(prefix) : -len(suffix)], "YAGA workflow path")
    if _required_environment("GITHUB_REF") != f"refs/heads/{default_branch}":
        raise GateError("YAGA ref is not the repository default branch")
    return workflow


def _write_outputs(route: str, *, pull_request_number: int | None = None) -> None:
    if route not in {"approved", "done", "external", "observe", "owner", "skip"}:
        raise GateError("YAGA route is invalid")
    output_path = _required_environment("GITHUB_OUTPUT")
    try:
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"route={route}\n")
            if pull_request_number is not None:
                stream.write(
                    "pull_request_number="
                    f"{positive_int(pull_request_number, 'pull request output')}\n"
                )
    except OSError as error:
        raise GateError("GITHUB_OUTPUT could not be written") from error


def run_action() -> int:
    """Execute one closed YAGA operation from trusted GitHub context."""
    started_at = time.monotonic()
    operation = operation_name(_required_environment("YAGA_OPERATION"))
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
    default_branch = _default_branch(event)
    _trusted_workflow_path(repository, default_branch)
    server_url = _server_url()
    run_id = _environment_positive_int("GITHUB_RUN_ID", "YAGA workflow run ID")
    run_attempt = _environment_positive_int("GITHUB_RUN_ATTEMPT", "YAGA workflow run attempt")
    target_url = _attempt_url(server_url, repository, run_id, run_attempt)
    timeout = request_timeout(_required_environment("YAGA_REQUEST_TIMEOUT"))
    job_deadline = started_at + _job_timeout_minutes() * 60
    poll_deadline = min(
        started_at + POLL_WINDOW_SECONDS,
        job_deadline - SUCCESS_WRITE_REQUEST_RESERVE * timeout - SUCCESS_CLEANUP_MARGIN_SECONDS,
    )
    if poll_deadline <= started_at:
        raise GateError("YAGA job timeout leaves no bounded polling window")
    operation_budgets = {
        "authorize": MAX_AUTHORIZATION_REQUESTS,
        "finalize": MAX_FINALIZATION_REQUESTS,
        "invalidate": MAX_INVALIDATION_REQUESTS,
        "observe": MAX_POLL_REQUESTS,
        "prepare": MAX_PREPARATION_REQUESTS,
        "request": MAX_POLL_REQUESTS,
    }
    api = GitHubRestApi(
        _required_environment("GITHUB_TOKEN"),
        base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        timeout=timeout,
        max_requests=min(operation_budgets[operation], MAX_API_REQUESTS),
        request_deadline=job_deadline - 15,
        success_deadline=job_deadline,
    )

    if operation == "invalidate":
        # Parse locally before the required native lifecycle job performs its
        # exact live-state and capacity checks.
        parse_event_boundary(repository=repository, event_name=event_name, event=event)
        result = invalidate(
            api,
            repository=repository,
            event_name=event_name,
            event=event,
            run_id=run_id,
            run_attempt=run_attempt,
            target_url=target_url,
        )
    else:
        prerequisite = workflow_path(
            _required_environment("YAGA_PREREQUISITE_WORKFLOW"),
            "prerequisite workflow",
        )
        lifecycle = workflow_path(
            _required_environment("YAGA_LIFECYCLE_WORKFLOW"),
            "lifecycle workflow",
        )
        source = load_source_from_wake(
            api,
            repository=repository,
            event_name=event_name,
            event=event,
            prerequisite_workflow=prerequisite,
            lifecycle_workflow=lifecycle,
        )
        if source is None:
            _write_outputs("skip")
            print(f"YAGA {operation}: skipped: paired CI and lifecycle are not both complete")
            return 0
        if operation == "prepare":
            result = prepare(
                api,
                repository=repository,
                source=source,
                lifecycle_workflow=lifecycle,
                wake_workflow=workflow_path(
                    record(event.get("workflow_run"), "workflow event run").get("path"),
                    "publisher wake workflow",
                ),
                server_url=server_url,
                target_url=target_url,
                direct_author_id=_environment_positive_int(
                    "YAGA_OWNER_ID",
                    "direct review author ID",
                ),
                lifecycle_deadline=min(
                    started_at + LIFECYCLE_WAIT_SECONDS,
                    poll_deadline,
                ),
            )
        elif operation == "authorize":
            if _required_environment("YAGA_APPROVAL_MARKER") != APPROVAL_ENVIRONMENT_MARKER:
                raise GateError("protected approval environment marker is invalid")
            result = authorize(
                api,
                repository=repository,
                source=source,
                lifecycle_workflow=lifecycle,
                server_url=server_url,
                direct_author_id=_environment_positive_int(
                    "YAGA_OWNER_ID",
                    "direct review author ID",
                ),
            )
        elif operation in {"observe", "request"}:
            result = review(
                api,
                repository=repository,
                source=source,
                lifecycle_workflow=lifecycle,
                server_url=server_url,
                target_url=target_url,
                allow_request=operation == "request",
                direct_author_id=_environment_positive_int(
                    "YAGA_OWNER_ID",
                    "direct review author ID",
                ),
                poll_deadline=poll_deadline,
            )
        else:
            result = finalize(
                api,
                repository=repository,
                source=source,
                lifecycle_workflow=lifecycle,
                server_url=server_url,
                target_url=target_url,
                direct_author_id=_environment_positive_int(
                    "YAGA_OWNER_ID",
                    "direct review author ID",
                ),
            )
    _write_outputs(
        result.route,
        pull_request_number=source.pull_request_number if operation == "prepare" else None,
    )
    print(f"YAGA {operation}: {result.message}")
    return result.exit_code
