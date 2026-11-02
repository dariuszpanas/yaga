"""Fetch one explicit pull-request review snapshot through a bounded installed gh CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yaga.agent_review.snapshot import (
    MAX_SNAPSHOT_RECORDS,
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
            raise ValueError("pull request host must be a bounded ASCII DNS hostname")
        if not isinstance(self.owner, str) or _OWNER.fullmatch(self.owner) is None:
            raise ValueError("pull request owner must be a bounded ASCII repository owner")
        if (
            not isinstance(self.repository, str)
            or _REPOSITORY.fullmatch(self.repository) is None
            or self.repository in {".", ".."}
        ):
            raise ValueError("pull request repository must be a bounded ASCII repository name")
        if type(self.number) is not int or not 1 <= self.number <= MAX_PULL_REQUEST_NUMBER:
            raise ValueError("pull request number must be a bounded positive integer")
        object.__setattr__(self, "host", self.host.lower())

    @property
    def url(self) -> str:
        """Return the selected canonical HTTPS PR URL."""
        return f"https://{self.host}/{self.owner}/{self.repository}/pull/{self.number}"

    @property
    def gh_repository(self) -> str:
        """Return an explicit gh repository selector, including its host."""
        return f"{self.host}/{self.owner}/{self.repository}"


def parse_pull_request_reference(value: str, repository: str | None = None) -> PullRequestReference:
    """Accept an explicit positive number plus repository, or one HTTPS PR URL."""
    _require_selector(value, label="--pr")
    if value.isdecimal():
        if _NUMBER.fullmatch(value) is None:
            raise InputError("--pr number must be positive ASCII digits without leading zeroes")
        number = _parse_number(value)
        if repository is None:
            raise InputError("a numeric --pr requires --repo [HOST/]OWNER/REPO")
        _require_selector(repository, label="--repo")
        parts = repository.split("/")
        if len(parts) == 2:
            parts.insert(0, "github.com")
        if len(parts) != 3:
            raise InputError("--repo must use [HOST/]OWNER/REPO")
        return _reference(*parts, number=number)
    if repository is not None:
        raise InputError("--repo is supported only with a numeric --pr, not a PR URL")
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.username is not None
            or url.password is not None
            or url.port is not None
            or url.netloc.casefold() != (url.hostname or "").casefold()
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
) -> PullRequestReviewSnapshot:
    """Fetch one current PR review snapshot using gh authentication, without Git or writes."""
    if not isinstance(reference, PullRequestReference):
        raise TypeError("remote review fetch requires a PullRequestReference")
    executable = _find_gh()
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

    # Live --pr fetches never derive reaction freshness from Git commit dates
    # or partially emulate the gate publisher's boundary/workflow-run state;
    # head_committed_at is None so live checks require head-bound review or
    # summary evidence to pass rather than unbound PR-body reactions.
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
    }
    return parse_pull_request_snapshot(snapshot_document, source=actual.url)


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
        return PullRequestReference(host, owner, repository, number)
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
