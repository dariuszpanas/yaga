"""Dependency-free, bounded GitHub REST transport and pull-request loading."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPMessage
from typing import IO, Any, Protocol, cast

from yaga.errors import GateError
from yaga.models import (
    PullRequest,
    commit_sha,
    positive_int,
    record,
    ref_name,
    repository_name,
    request_timeout,
    timestamp,
)

API_VERSION = "2022-11-28"
USER_AGENT = "yaga-action"
MAX_API_PAGES = 10
MAX_API_RECORDS = 1_000
MAX_API_REQUESTS = 300
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BODY_BYTES = 4_096
MAX_API_URL_BYTES = 2_048
MAX_API_PATH_BYTES = 4_096


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Prevent an API redirect from forwarding the authorization header."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def _open_without_redirects(request: urllib.request.Request, *, timeout: float) -> Any:
    """Open one request without following even same-origin redirects."""
    return urllib.request.build_opener(_RejectRedirects()).open(request, timeout=timeout)


class RestApi(Protocol):
    """The small GitHub REST surface used by gate components."""

    def get(self, path: str) -> object: ...

    def post(self, path: str, payload: dict[str, object]) -> object: ...

    def paginate(self, path: str) -> list[dict[str, Any]]: ...


class GitHubRestApi:
    """Authenticated GitHub REST client with bounded responses and pagination."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = 15.0,
        max_requests: int = MAX_API_REQUESTS,
        success_deadline: float | None = None,
    ) -> None:
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 4_096
            or any(not 33 <= ord(character) <= 126 for character in token)
        ):
            raise GateError("GITHUB_TOKEN is invalid")
        if (
            not isinstance(base_url, str)
            or not base_url
            or any(not 33 <= ord(character) <= 126 for character in base_url)
            or len(base_url.encode("utf-8")) > MAX_API_URL_BYTES
        ):
            raise GateError("GITHUB_API_URL must be an HTTPS origin or base path")
        try:
            parsed_url = urllib.parse.urlsplit(base_url)
            hostname = parsed_url.hostname
            port = parsed_url.port
        except ValueError as error:
            raise GateError("GITHUB_API_URL must be an HTTPS origin or base path") from error
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or not hostname
            or (port is not None and not 1 <= port <= 65_535)
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise GateError("GITHUB_API_URL must be an HTTPS origin or base path")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = request_timeout(timeout)
        if (
            isinstance(max_requests, bool)
            or not isinstance(max_requests, int)
            or not 1 <= max_requests <= MAX_API_REQUESTS
        ):
            raise GateError("GitHub API request budget is invalid")
        self._max_requests = max_requests
        self._request_count = 0
        if success_deadline is not None and (
            isinstance(success_deadline, bool)
            or not isinstance(success_deadline, (int, float))
            or not math.isfinite(success_deadline)
        ):
            raise GateError("success deadline is invalid")
        self._success_deadline = success_deadline

    def _consume_request_budget(self) -> None:
        if self._request_count >= self._max_requests:
            raise GateError("GitHub API request budget exhausted")
        self._request_count += 1

    def require_success_tail(self, count: int, *, cleanup_margin_seconds: int) -> None:
        """Admit success only while its bounded request and time tail still fits."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise GateError("GitHub API request reserve is invalid")
        if (
            isinstance(cleanup_margin_seconds, bool)
            or not isinstance(cleanup_margin_seconds, int)
            or cleanup_margin_seconds < 1
        ):
            raise GateError("success cleanup margin is invalid")
        if self._request_count + count > self._max_requests:
            raise GateError("GitHub API request reserve is unavailable")
        if self._success_deadline is None:
            raise GateError("success deadline is unavailable")
        required_seconds = count * self._timeout + cleanup_margin_seconds
        if time.monotonic() + required_seconds > self._success_deadline:
            raise GateError("success validation tail no longer fits the job deadline")

    def _request_json(
        self,
        path: str,
        *,
        method: str,
        payload: dict[str, object] | None = None,
    ) -> object:
        """Send one bounded GitHub REST request and decode its response."""
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or "#" in path
            or any(ord(character) <= 32 or ord(character) >= 127 for character in path)
            or len(path.encode("utf-8")) > MAX_API_PATH_BYTES
        ):
            raise GateError("GitHub API path is invalid")
        if method not in {"GET", "POST"} or (method == "GET") != (payload is None):
            raise GateError("GitHub API request method is invalid")
        request_body = None
        if payload is not None:
            request_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if len(request_body) > MAX_REQUEST_BODY_BYTES:
                raise GateError("GitHub API request exceeded the byte limit")
        self._consume_request_budget()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION,
        }
        if request_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            request = urllib.request.Request(
                f"{self._base_url}{path}",
                data=request_body,
                method=method,
                headers=headers,
            )
        except (TypeError, ValueError) as error:
            raise GateError("GitHub API request could not be constructed") from error
        try:
            with _open_without_redirects(request, timeout=self._timeout) as response:
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise GateError(f"GitHub API {method} failed with HTTP {error.code}") from error
        except ValueError as error:
            raise GateError(f"GitHub API {method} failed before transport") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            detail = " ".join(str(getattr(error, "reason", error)).split())[:300]
            raise GateError(f"GitHub API {method} failed: {detail}") from error
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise GateError("GitHub API response exceeded the byte limit")
        try:
            return json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GateError("GitHub API returned invalid JSON") from error

    def get(self, path: str) -> object:
        """Read and decode one bounded GitHub REST response."""
        return self._request_json(path, method="GET")

    def post(self, path: str, payload: dict[str, object]) -> object:
        """Create one bounded GitHub REST resource."""
        return self._request_json(path, method="POST", payload=payload)

    def paginate(self, path: str) -> list[dict[str, Any]]:
        """Read at most ten 100-record pages from one list endpoint."""
        separator = "&" if "?" in path else "?"
        records: list[dict[str, Any]] = []
        for page in range(1, MAX_API_PAGES + 1):
            response = self.get(f"{path}{separator}per_page=100&page={page}")
            if not isinstance(response, list) or not all(
                isinstance(item, dict) for item in response
            ):
                raise GateError("GitHub API pagination returned an invalid page")
            page_records = cast(list[dict[str, Any]], response)
            records.extend(page_records)
            if len(records) > MAX_API_RECORDS:
                raise GateError("GitHub API pagination exceeded the record limit")
            if len(page_records) < 100:
                return records
        raise GateError("GitHub API pagination exceeded the page limit")


def load_default_branch(api: RestApi, repository: str) -> str:
    """Load the repository's live default branch from an exact repository record."""
    repository = repository_name(repository)
    payload = record(api.get(f"/repos/{repository}"), "repository")
    if payload.get("full_name") != repository:
        raise GateError("GitHub returned a different repository")
    return ref_name(payload.get("default_branch"), "repository default branch")


def require_success_tail(
    api: RestApi,
    count: int,
    *,
    cleanup_margin_seconds: int,
) -> None:
    """Apply success admission when the concrete runtime exposes it."""
    checker = getattr(api, "require_success_tail", None)
    if checker is not None:
        checker(count, cleanup_margin_seconds=cleanup_margin_seconds)


def parse_pull_request(
    value: object,
    *,
    repository: str,
    expected_number: int | None = None,
) -> PullRequest:
    """Strictly parse one pull-request record returned by GitHub."""
    repository = repository_name(repository)
    if expected_number is not None:
        expected_number = positive_int(expected_number, "pull request number")
    payload = record(value, "pull request")
    payload_number = positive_int(payload.get("number"), "pull request number")
    if expected_number is not None and payload_number != expected_number:
        raise GateError("GitHub returned a different pull request")
    head = record(payload.get("head"), "pull request head")
    head_repository = record(head.get("repo"), "pull request head repository")
    base = record(payload.get("base"), "pull request base")
    base_repository = record(base.get("repo"), "pull request base repository")
    draft = payload.get("draft")
    state = payload.get("state")
    if not isinstance(draft, bool):
        raise GateError("pull request draft state is invalid")
    if state not in {"open", "closed"}:
        raise GateError("pull request state is invalid")
    if repository_name(base_repository.get("full_name")) != repository:
        raise GateError("pull request base repository does not match the requested repository")
    return PullRequest(
        number=payload_number,
        head_sha=commit_sha(head.get("sha"), "pull request head"),
        head_ref=ref_name(head.get("ref"), "pull request head ref"),
        head_repository=repository_name(head_repository.get("full_name")),
        base_ref=ref_name(base.get("ref"), "pull request base ref"),
        base_sha=commit_sha(base.get("sha"), "pull request base"),
        draft=draft,
        state=state,
        created_at=timestamp(payload.get("created_at"), "pull request creation"),
    )


def load_pull_request(api: RestApi, repository: str, number: int) -> PullRequest:
    """Load and strictly parse one live pull request."""
    repository = repository_name(repository)
    number = positive_int(number, "pull request number")
    return parse_pull_request(
        api.get(f"/repos/{repository}/pulls/{number}"),
        repository=repository,
        expected_number=number,
    )
