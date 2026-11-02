"""Fetch and submit explicit pull-request review state through a bounded installed gh CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yaga.agent_review.policy import MAX_DELAY_SECONDS, MAX_WAIT_SECONDS
from yaga.agent_review.snapshot import (
    MAX_SNAPSHOT_RECORDS,
    PullRequestReviewRecord,
    PullRequestReviewSnapshot,
    parse_pull_request_snapshot,
)
from yaga.errors import InputError
from yaga.git.runtime import run_bounded_process

MAX_PULL_REQUEST_NUMBER = 2**63 - 1
MAX_REFERENCE_CHARACTERS = 1024
MAX_GH_STDOUT_BYTES = 512 * 1024
MAX_GH_STDERR_BYTES = 64 * 1024
GH_TIMEOUT_SECONDS = 30
POLL_INTERVAL_SECONDS = 15
STARTUP_GRACE_SECONDS = 30
GITHUB_ACTIONS_LOGIN = "github-actions[bot]"
GITHUB_ACTIONS_USER_ID = 41_898_282
_YAGA_REVIEW_MARKER = "<!-- yaga-pull-request-review:v1 -->"
_EXPECTED_REVIEW_STATES = {
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
}

_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_NUMBER = re.compile(r"[1-9][0-9]{0,18}")
_URL_PATH = re.compile(r"/([^/]+)/([^/]+)/pull/([1-9][0-9]{0,18})/?")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True, slots=True)
class PullRequestReference:
    """A fully selected host, repository, and PR number, with no local inference."""

    host: str
    owner: str
    repository: str
    number: int

    def __post_init__(self) -> None:
        if not _valid_host(self.host):
            raise ValueError("invalid GitHub host")
        if _OWNER.fullmatch(self.owner) is None:
            raise ValueError("invalid GitHub owner")
        if _REPOSITORY.fullmatch(self.repository) is None or self.repository in {".", ".."}:
            raise ValueError("invalid GitHub repository")
        if (
            isinstance(self.number, bool)
            or not isinstance(self.number, int)
            or not 1 <= self.number <= MAX_PULL_REQUEST_NUMBER
        ):
            raise ValueError("invalid pull request number")

    @property
    def gh_repository(self) -> str:
        """Return the explicit HOST/OWNER/REPO selector passed to gh."""
        return f"{self.host.lower()}/{self.owner}/{self.repository}"

    @property
    def url(self) -> str:
        """Return the canonical HTTPS pull request URL."""
        return f"https://{self.host.lower()}/{self.owner}/{self.repository}/pull/{self.number}"


@dataclass(frozen=True, slots=True)
class SubmittedPullRequestReview:
    """Result of an opt-in commit-bound pull-request review submission."""

    action: str
    event: str
    state: str
    review_id: int
    commit_id: str

    def document(self) -> dict[str, Any]:
        """Return the JSON report representation of this review submission."""
        return {
            "action": self.action,
            "event": self.event,
            "state": self.state,
            "review_id": self.review_id,
            "commit_id": self.commit_id,
        }


def sleep_for_review_poll(seconds: int) -> None:
    """Sleep for a bounded poll interval while waiting for an active PR review."""
    if type(seconds) is not int or not 1 <= seconds <= MAX_WAIT_SECONDS:
        raise ValueError(f"poll sleep seconds must be an integer between 1 and {MAX_WAIT_SECONDS}")
    time.sleep(seconds)


def parse_pull_request_reference(
    pull_request: str,
    repository: str | None = None,
) -> PullRequestReference:
    """Parse one explicit PR number + repository or canonical HTTPS PR URL."""
    _require_selector(pull_request, label="--pr")
    value = pull_request
    if _NUMBER.fullmatch(value) is not None:
        if repository is None:
            raise InputError(
                "numeric --pr requires --repo [HOST/]OWNER/REPO or an HTTPS pull request URL"
            )
        _require_selector(repository, label="--repo")
        parts = repository.split("/")
        if len(parts) == 2:
            host, owner, repo = "github.com", parts[0], parts[1]
        elif len(parts) == 3:
            host, owner, repo = parts
        else:
            raise InputError("--repo must be OWNER/REPO or HOST/OWNER/REPO")
        return _reference(host, owner, repo, number=_parse_number(value))
    if repository is not None:
        raise InputError("--repo cannot be combined with a pull request URL in --pr")
    try:
        url = urlsplit(value)
        if (
            url.scheme.lower() != "https"
            or not url.netloc
            or url.username is not None
            or url.password is not None
            or url.port is not None
            or url.query
            or url.fragment
            or "?" in value
            or "#" in value
            or "%" in value
        ):
            raise ValueError("unsupported URL structure")
        path = _URL_PATH.fullmatch(url.path)
        if path is None or url.hostname is None:
            raise ValueError("unsupported PR URL path")
        return _reference(url.hostname, path[1], path[2], number=_parse_number(path[3]))
    except ValueError:
        raise InputError(
            "--pr must be a positive ASCII number with --repo, or an HTTPS "
            "HOST/OWNER/REPO/pull/NUMBER URL without credentials, ports, query or fragment"
        ) from None


def fetch_pull_request_review_snapshot(
    reference: PullRequestReference,
    *,
    plan_digest: str,
    include_check_runs: bool = False,
    delay_seconds: int = 0,
) -> PullRequestReviewSnapshot:
    """Fetch one current PR review snapshot using gh authentication, without Git or writes."""
    if not isinstance(reference, PullRequestReference):
        raise TypeError("remote review fetch requires a PullRequestReference")
    if type(delay_seconds) is not int or not 0 <= delay_seconds <= MAX_DELAY_SECONDS:
        raise ValueError("delay_seconds must be an integer between 0 and 300")
    executable = _find_gh()
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    base_endpoint = f"repos/{reference.owner}/{reference.repository}"

    pr_doc = _gh_api_json(executable, reference, f"{base_endpoint}/pulls/{reference.number}")
    if not isinstance(pr_doc, dict):
        raise InputError("gh returned an unexpected pull-request metadata document")
    if type(pr_doc.get("number")) is not int or pr_doc["number"] != reference.number:
        raise InputError("gh returned a different or invalid pull-request number")
    html_url = pr_doc.get("html_url")
    if not isinstance(html_url, str):
        raise InputError("gh returned an invalid pull-request URL")
    try:
        actual = parse_pull_request_reference(html_url)
    except InputError:
        raise InputError("gh returned an invalid pull-request URL") from None
    if (
        actual.number != reference.number
        or actual.gh_repository.casefold() != reference.gh_repository.casefold()
    ):
        raise InputError("gh returned a different pull-request identity")

    state = pr_doc.get("state")
    if not isinstance(state, str) or state not in {"open", "closed"}:
        raise InputError("gh returned an invalid pull-request state")
    draft = pr_doc.get("draft", False)
    if not isinstance(draft, bool):
        raise InputError("gh returned an invalid pull-request draft flag")
    head_sha = _extract_branch_sha(pr_doc.get("head"), label="head")
    base_sha = _extract_branch_sha(pr_doc.get("base"), label="base")

    reviews_doc = _gh_api_json(
        executable,
        reference,
        f"{base_endpoint}/pulls/{reference.number}/reviews?per_page={MAX_SNAPSHOT_RECORDS}",
    )
    review_comments_doc = _gh_api_json(
        executable,
        reference,
        f"{base_endpoint}/pulls/{reference.number}/comments?per_page={MAX_SNAPSHOT_RECORDS}",
    )
    issue_comments_doc = _gh_api_json(
        executable,
        reference,
        f"{base_endpoint}/issues/{reference.number}/comments?per_page={MAX_SNAPSHOT_RECORDS}",
    )
    reactions_doc = _gh_api_json(
        executable,
        reference,
        f"{base_endpoint}/issues/{reference.number}/reactions?per_page={MAX_SNAPSHOT_RECORDS}",
    )
    check_runs_items: list[dict[str, Any]] = []
    if include_check_runs:
        check_runs_doc = _gh_api_json(
            executable,
            reference,
            f"{base_endpoint}/commits/{head_sha}/check-runs?per_page={MAX_SNAPSHOT_RECORDS}",
        )
        check_runs_items = _normalize_check_runs(check_runs_doc)

    # Live --pr fetches never derive reaction freshness from Git commit dates
    # or partially emulate the gate publisher's boundary/workflow-run state;
    # head_committed_at is None so live checks require head-bound review,
    # summary, or check-run evidence to pass rather than unbound PR-body reactions.
    snapshot_document: dict[str, Any] = {
        "version": 1,
        "plan_digest": plan_digest,
        "number": actual.number,
        "url": actual.url,
        "state": state,
        "draft": draft,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "head_committed_at": None,
        "reviews": _normalize_reviews(reviews_doc),
        "review_comments": _normalize_review_comments(review_comments_doc),
        "issue_comments": _normalize_issue_comments(issue_comments_doc),
        "reactions": _normalize_reactions(reactions_doc),
        "check_runs": check_runs_items,
    }
    return parse_pull_request_snapshot(snapshot_document, source=actual.url)


def submit_pull_request_review(
    reference: PullRequestReference,
    *,
    head_sha: str,
    event: str,
    existing_reviews: tuple[PullRequestReviewRecord, ...] = (),
) -> SubmittedPullRequestReview:
    """Submit an idempotent commit-bound APPROVE or REQUEST_CHANGES review on head_sha."""
    if not isinstance(reference, PullRequestReference):
        raise TypeError("review submission requires a PullRequestReference")
    if not isinstance(head_sha, str) or _SHA.fullmatch(head_sha) is None:
        raise ValueError("head_sha must be a 40-character hexadecimal SHA")
    normalized_sha = head_sha.lower()
    expected_state = _EXPECTED_REVIEW_STATES.get(event)
    if expected_state is None:
        raise ValueError("event must be APPROVE or REQUEST_CHANGES")

    executable = _find_gh()
    marked_reviews = [
        review
        for review in existing_reviews
        if _YAGA_REVIEW_MARKER in review.body and review.state in {"APPROVED", "CHANGES_REQUESTED"}
    ]
    has_matching_target = any(
        review.commit_id == normalized_sha and review.state == expected_state
        for review in marked_reviews
    )
    if has_matching_target:
        actor = _resolve_review_actor(executable, reference)
        if actor is not None:
            actor_login, actor_id = actor
            actor_reviews = [
                review
                for review in marked_reviews
                if review.author_login.casefold() == actor_login.casefold()
                and review.author_id == actor_id
            ]
            if actor_reviews:
                latest = max(
                    actor_reviews,
                    key=lambda item: (
                        item.submitted_at or datetime.min.replace(tzinfo=UTC),
                        item.review_id,
                    ),
                )
                if latest.commit_id == normalized_sha and latest.state == expected_state:
                    return SubmittedPullRequestReview(
                        action="unchanged",
                        event=event,
                        state=expected_state,
                        review_id=latest.review_id,
                        commit_id=normalized_sha,
                    )

    verdict = "passed" if event == "APPROVE" else "failed"
    body = (
        f"YAGA pull-request review check {verdict} for commit `{normalized_sha[:12]}`.\n\n"
        f"{_YAGA_REVIEW_MARKER}"
    )
    endpoint = f"repos/{reference.owner}/{reference.repository}/pulls/{reference.number}/reviews"
    created = _gh_api_post_json(
        executable,
        reference,
        endpoint,
        fields=(
            ("commit_id", normalized_sha),
            ("event", event),
            ("body", body),
        ),
    )
    if not isinstance(created, dict):
        raise InputError("gh returned an invalid pull-request review submission document")
    review_id = created.get("id")
    if type(review_id) is not int or review_id < 1:
        raise InputError("gh returned an invalid pull-request review id")
    state = created.get("state")
    if state != expected_state:
        raise InputError("gh returned an unexpected pull-request review state")
    commit_id = created.get("commit_id")
    if not isinstance(commit_id, str) or commit_id.lower() != normalized_sha:
        raise InputError("gh returned a pull-request review bound to a different commit_id")
    return SubmittedPullRequestReview(
        action="submitted",
        event=event,
        state=expected_state,
        review_id=review_id,
        commit_id=normalized_sha,
    )


def _resolve_review_actor(
    executable: str,
    reference: PullRequestReference,
) -> tuple[str, int] | None:
    try:
        user_doc = _gh_api_json(executable, reference, "user")
    except InputError:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            return GITHUB_ACTIONS_LOGIN, GITHUB_ACTIONS_USER_ID
        return None
    if isinstance(user_doc, dict):
        login = user_doc.get("login")
        user_id = user_doc.get("id")
        if isinstance(login, str) and login and type(user_id) is int and user_id >= 1:
            return login, user_id
    return None


def _gh_api_json(
    executable: str,
    reference: PullRequestReference,
    endpoint: str,
) -> Any:
    try:
        result = run_bounded_process(
            [
                executable,
                "api",
                "--hostname",
                reference.host,
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "X-GitHub-Api-Version: 2022-11-28",
                endpoint,
            ],
            stdout_limit=MAX_GH_STDOUT_BYTES,
            stderr_limit=MAX_GH_STDERR_BYTES,
            timeout_seconds=GH_TIMEOUT_SECONDS,
            environment=_gh_environment(),
        )
    except (OSError, ValueError):
        raise InputError("gh could not execute its bounded pull-request review request") from None
    if result.timed_out:
        raise InputError("gh exceeded the 30-second pull-request review timeout")
    if result.stdout_overflow or result.stderr_overflow:
        raise InputError("gh exceeded the bounded pull-request review output limit")
    if result.returncode != 0:
        raise InputError(
            "gh could not fetch the pull-request review state; check gh authentication, "
            "repository access, PR existence, and network connectivity"
        )
    if len(result.stdout) > MAX_GH_STDOUT_BYTES:
        raise InputError("gh exceeded the bounded pull-request review JSON limit")
    try:
        return json.loads(
            result.stdout.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        raise InputError("gh returned invalid or ambiguous UTF-8 JSON") from None


def _gh_api_post_json(
    executable: str,
    reference: PullRequestReference,
    endpoint: str,
    *,
    fields: tuple[tuple[str, str], ...],
) -> Any:
    command = [
        executable,
        "api",
        "--hostname",
        reference.host,
        "--method",
        "POST",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
    ]
    for key, value in fields:
        command.extend(("-f", f"{key}={value}"))
    command.append(endpoint)
    try:
        result = run_bounded_process(
            command,
            stdout_limit=MAX_GH_STDOUT_BYTES,
            stderr_limit=MAX_GH_STDERR_BYTES,
            timeout_seconds=GH_TIMEOUT_SECONDS,
            environment=_gh_environment(),
        )
    except (OSError, ValueError):
        raise InputError(
            "gh could not execute its bounded pull-request review submission"
        ) from None
    if result.timed_out:
        raise InputError("gh exceeded the 30-second pull-request review submission timeout")
    if result.stdout_overflow or result.stderr_overflow:
        raise InputError("gh exceeded the bounded pull-request review submission output limit")
    if result.returncode != 0:
        raise InputError(
            "gh could not submit the pull-request review; check gh authentication, "
            "pull-requests: write permission, and repository Actions approval settings"
        )
    if len(result.stdout) > MAX_GH_STDOUT_BYTES:
        raise InputError("gh exceeded the bounded pull-request review submission JSON limit")
    try:
        return json.loads(
            result.stdout.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        raise InputError("gh returned invalid or ambiguous UTF-8 JSON") from None


def _extract_branch_sha(raw: Any, *, label: str) -> str:
    if not isinstance(raw, dict):
        raise InputError(f"gh returned an invalid pull-request {label} object")
    sha = raw.get("sha")
    if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
        raise InputError(f"gh returned an invalid pull-request {label}.sha")
    return sha.lower()


def _normalize_reviews(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise InputError("gh returned a non-list pull-request reviews payload")
    items: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise InputError("gh returned a non-object pull-request review entry")
        login, user_id = _extract_user(entry.get("user"), label="review")
        body = entry.get("body")
        if body is None:
            body = ""
        commit_id = entry.get("commit_id")
        if isinstance(commit_id, str):
            commit_id = commit_id.lower()
        items.append(
            {
                "id": entry.get("id"),
                "author_login": login,
                "author_id": user_id,
                "state": entry.get("state"),
                "commit_id": commit_id,
                "submitted_at": entry.get("submitted_at"),
                "body": body,
            }
        )
    return items


def _normalize_review_comments(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise InputError("gh returned a non-list pull-request review comments payload")
    items: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise InputError("gh returned a non-object pull-request review comment entry")
        login, user_id = _extract_user(entry.get("user"), label="review comment")
        body = entry.get("body")
        if body is None:
            body = ""
        commit_id = entry.get("commit_id")
        if isinstance(commit_id, str):
            commit_id = commit_id.lower()
        items.append(
            {
                "id": entry.get("id"),
                "pull_request_review_id": entry.get("pull_request_review_id"),
                "author_login": login,
                "author_id": user_id,
                "commit_id": commit_id,
                "created_at": entry.get("created_at"),
                "path": entry.get("path"),
                "body": body,
            }
        )
    return items


def _normalize_issue_comments(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise InputError("gh returned a non-list pull-request issue comments payload")
    items: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise InputError("gh returned a non-object pull-request issue comment entry")
        login, user_id = _extract_user(entry.get("user"), label="issue comment")
        app_raw = entry.get("performed_via_github_app")
        app_id = None
        if app_raw is not None:
            if not isinstance(app_raw, dict):
                raise InputError("gh returned an invalid performed_via_github_app field")
            app_id = app_raw.get("id")
        body = entry.get("body")
        if body is None:
            body = ""
        items.append(
            {
                "id": entry.get("id"),
                "author_login": login,
                "author_id": user_id,
                "app_id": app_id,
                "created_at": entry.get("created_at"),
                "updated_at": entry.get("updated_at"),
                "body": body,
            }
        )
    return items


def _normalize_reactions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise InputError("gh returned a non-list pull-request reactions payload")
    items: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise InputError("gh returned a non-object pull-request reaction entry")
        login, user_id = _extract_user(entry.get("user"), label="reaction")
        items.append(
            {
                "id": entry.get("id"),
                "author_login": login,
                "author_id": user_id,
                "content": entry.get("content"),
                "created_at": entry.get("created_at"),
            }
        )
    return items


def _normalize_check_runs(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        raise InputError("gh returned a non-object commit check-runs payload")
    runs_raw = raw.get("check_runs")
    if not isinstance(runs_raw, list):
        raise InputError("gh returned a non-list commit check_runs field")
    total_count = raw.get("total_count")
    if (
        type(total_count) is not int
        or total_count < len(runs_raw)
        or total_count >= MAX_SNAPSHOT_RECORDS
        or len(runs_raw) >= MAX_SNAPSHOT_RECORDS
    ):
        raise InputError("gh returned an invalid or truncated commit check-runs count")
    items: list[dict[str, Any]] = []
    for entry in runs_raw:
        if not isinstance(entry, dict):
            raise InputError("gh returned a non-object commit check-run entry")
        app_raw = entry.get("app")
        app_id = None
        app_slug = None
        if app_raw is not None:
            if not isinstance(app_raw, dict):
                raise InputError("gh returned an invalid check-run app field")
            app_id = app_raw.get("id")
            app_slug = app_raw.get("slug")
        head_sha = entry.get("head_sha")
        if isinstance(head_sha, str):
            head_sha = head_sha.lower()
        items.append(
            {
                "id": entry.get("id"),
                "name": entry.get("name"),
                "status": entry.get("status"),
                "conclusion": entry.get("conclusion"),
                "head_sha": head_sha,
                "app_id": app_id,
                "app_slug": app_slug,
                "started_at": entry.get("started_at"),
                "completed_at": entry.get("completed_at"),
            }
        )
    return items


def _extract_user(raw: Any, *, label: str) -> tuple[Any, Any]:
    if not isinstance(raw, dict):
        raise InputError(f"gh returned a {label} without an author user object")
    return raw.get("login"), raw.get("id")


def _require_selector(value: str, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_REFERENCE_CHARACTERS
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise InputError(f"{label} must be bounded printable ASCII without whitespace")


def _valid_host(value: str) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 253
        and all(_HOST_LABEL.fullmatch(label) is not None for label in value.split("."))
    )


def _parse_number(value: str) -> int:
    number = int(value)
    if not 1 <= number <= MAX_PULL_REQUEST_NUMBER:
        raise InputError("pull request number exceeds the supported positive integer range")
    return number


def _reference(host: str, owner: str, repository: str, *, number: int) -> PullRequestReference:
    try:
        return PullRequestReference(host.lower(), owner, repository, number)
    except ValueError:
        raise InputError(
            "pull request host, owner, or repository has an unsupported format"
        ) from None


def _find_gh() -> str:
    try:
        executable = shutil.which("gh")
    except (OSError, ValueError):
        raise InputError("gh executable could not be located safely") from None
    if executable is None:
        raise InputError("checking --pr requires the GitHub CLI (gh); install gh and authenticate")
    try:
        original = Path(executable)
        if not original.is_absolute():
            raise ValueError("relative executable")
        resolved = original.resolve(strict=True)
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise ValueError("nonregular executable")
        current_directory = Path.cwd().resolve(strict=True)
        if (
            original.parent.resolve(strict=True) == current_directory
            or resolved.parent == current_directory
        ):
            raise ValueError("current-directory executable")
        if os.name == "nt" and any(
            path.suffix.casefold() != ".exe" for path in (original, resolved)
        ):
            raise ValueError("Windows executable must use .exe")
        return str(resolved)
    except (OSError, RuntimeError, ValueError):
        raise InputError(
            "gh must resolve to an absolute regular executable (gh.exe on Windows) "
            "installed on PATH outside the current directory"
        ) from None


def _gh_environment() -> dict[str, str]:
    excluded = {"GH_DEBUG", "DEBUG", "GH_FORCE_TTY", "GH_HOST", "GH_REPO"}
    environment = {key: value for key, value in os.environ.items() if key.upper() not in excluded}
    environment.update(
        {
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
            "GH_PAGER": "",
            "PAGER": "",
            "GH_TELEMETRY": "false",
            "NO_COLOR": "1",
            "CLICOLOR": "0",
            "CLICOLOR_FORCE": "0",
        }
    )
    return environment


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _reject_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON value")
