"""Tests for the bounded GitHub REST client and live PR parsing."""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from http.client import HTTPMessage
from typing import Any

import pytest

import yaga.github as github
from yaga.errors import GateError

REPOSITORY = "owner/repository"
PULL_REQUEST = 17
HEAD = "a" * 40
BASE = "c" * 40


class FakeResponse:
    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body
        self.read_limits: list[int] = []

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        self.read_limits.append(limit)
        return self.body


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.github.com",
        "https://user@example.com",
        "https://user:password@example.com",
        "https://api.github.com?query=1",
        "https://api.github.com#fragment",
        "https://api.github.com:99999",
        "https://[invalid",
        "not-a-url",
    ],
)
def test_rest_client_rejects_unsafe_api_base_urls(base_url: str) -> None:
    with pytest.raises(GateError, match="HTTPS origin"):
        github.GitHubRestApi("token", base_url=base_url)


def test_rest_client_requires_a_token_without_disclosing_it() -> None:
    with pytest.raises(GateError, match="GITHUB_TOKEN is invalid") as raised:
        github.GitHubRestApi("")
    assert "token-value" not in str(raised.value)

    secret = "token-value\nunsafe"
    with pytest.raises(GateError, match="GITHUB_TOKEN is invalid") as raised:
        github.GitHubRestApi(secret)
    assert secret not in str(raised.value)

    for secret in ("token-\u0085-value", "token-ü-value"):
        with pytest.raises(GateError, match="GITHUB_TOKEN is invalid") as raised:
            github.GitHubRestApi(secret)
        assert secret not in str(raised.value)


def test_rest_client_enforces_one_budget_across_get_and_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbound_requests = 0

    def open_request(*_args: object, **_kwargs: object) -> FakeResponse:
        nonlocal outbound_requests
        outbound_requests += 1
        return FakeResponse()

    monkeypatch.setattr(github, "_open_without_redirects", open_request)
    api = github.GitHubRestApi("token")
    api._request_count = github.MAX_API_REQUESTS - 1

    assert api.get("/rate_limit") == {}
    with pytest.raises(GateError, match="request budget exhausted"):
        api.post("/example", {"value": 1})
    assert outbound_requests == 1


def test_rest_client_can_reserve_a_complete_compensating_write_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github.time, "monotonic", lambda: 100.0)
    api = github.GitHubRestApi("token", max_requests=10, success_deadline=1_000.0)
    api._request_count = 2

    api.require_success_tail(8, cleanup_margin_seconds=60)
    with pytest.raises(GateError, match="reserve is unavailable"):
        api.require_success_tail(9, cleanup_margin_seconds=60)


def test_rest_client_success_cutoff_fits_the_max_request_timeout_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(github.time, "monotonic", lambda: now)
    request_count = 9
    request_timeout = 30
    cleanup_margin = 60
    deadline = now + request_count * request_timeout + cleanup_margin
    api = github.GitHubRestApi(
        "token",
        timeout=request_timeout,
        max_requests=request_count,
        success_deadline=deadline,
    )

    api.require_success_tail(request_count, cleanup_margin_seconds=cleanup_margin)
    now += 0.001
    with pytest.raises(GateError, match="job deadline"):
        api.require_success_tail(request_count, cleanup_margin_seconds=cleanup_margin)


def test_rest_client_posts_compact_bounded_json(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[Any] = []
    response = FakeResponse()

    def open_request(request: Any, **_kwargs: object) -> FakeResponse:
        observed.append(request)
        return response

    monkeypatch.setattr(github, "_open_without_redirects", open_request)
    api = github.GitHubRestApi("secret-token")
    payload: dict[str, object] = {"state": "pending", "context": "Codex Review"}

    assert api.post("/repos/owner/repository/statuses/abc", payload) == {}
    request = observed[0]
    assert request.method == "POST"
    assert request.data == b'{"state":"pending","context":"Codex Review"}'
    assert request.headers["Authorization"] == "Bearer secret-token"
    assert request.headers["User-agent"] == github.USER_AGENT
    assert request.headers["Content-type"] == "application/json"
    assert response.read_limits == [github.MAX_RESPONSE_BYTES + 1]

    with pytest.raises(GateError, match="byte limit"):
        api.post("/example", {"description": "x" * github.MAX_REQUEST_BODY_BYTES})


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "//other-host/path",
        "/line\nbreak",
        "/tab\tpath",
        "/nul\x00path",
        "/delete\x7fpath",
        "/space path",
        "/a#b",
    ],
)
def test_rest_client_rejects_unsafe_paths_before_network(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    def unexpected_request(*_args: object, **_kwargs: object) -> FakeResponse:
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(github, "_open_without_redirects", unexpected_request)
    with pytest.raises(GateError, match="path is invalid"):
        github.GitHubRestApi("token").get(path)


def test_rest_client_bounds_responses_and_sanitizes_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "MAX_RESPONSE_BYTES", 3)
    monkeypatch.setattr(
        github, "_open_without_redirects", lambda *_args, **_kwargs: FakeResponse(b"1234")
    )
    with pytest.raises(GateError, match="response exceeded"):
        github.GitHubRestApi("token").get("/example")

    def fail(*_args: object, **_kwargs: object) -> FakeResponse:
        raise urllib.error.URLError("connection\nfailed")

    monkeypatch.setattr(github, "_open_without_redirects", fail)
    with pytest.raises(GateError, match="connection failed"):
        github.GitHubRestApi("token").get("/example")


def test_redirect_handler_never_constructs_a_followup_request() -> None:
    original = urllib.request.Request(
        "https://api.github.com/example",
        headers={"Authorization": "Bearer token-value"},
    )

    redirected = github._RejectRedirects().redirect_request(
        original,
        io.BytesIO(),
        302,
        "Found",
        HTTPMessage(),
        "https://attacker.example/collect",
    )

    assert redirected is None


class PagingApi(github.GitHubRestApi):
    def __init__(self, pages: Sequence[object]) -> None:
        self.pages = pages
        self.paths: list[str] = []

    def get(self, path: str) -> object:
        self.paths.append(path)
        return self.pages[len(self.paths) - 1]


def test_rest_pagination_is_bounded_and_requests_explicit_pages() -> None:
    api = PagingApi([[{"id": index} for index in range(100)], [{"id": 101}]])

    assert len(api.paginate("/example?state=open")) == 101
    assert api.paths == [
        "/example?state=open&per_page=100&page=1",
        "/example?state=open&per_page=100&page=2",
    ]

    full_pages = [[{"id": index} for index in range(100)] for _ in range(10)]
    with pytest.raises(GateError, match="page limit"):
        PagingApi(full_pages).paginate("/example")
    with pytest.raises(GateError, match="invalid page"):
        PagingApi([{"not": "a list"}]).paginate("/example")


def pull_request_payload() -> dict[str, object]:
    return {
        "number": PULL_REQUEST,
        "head": {
            "sha": HEAD,
            "ref": "feat/gate",
            "repo": {"full_name": REPOSITORY},
        },
        "base": {
            "ref": "main",
            "sha": BASE,
            "repo": {"full_name": REPOSITORY},
        },
        "user": {"id": 42, "login": "contributor"},
        "draft": False,
        "state": "open",
        "created_at": "2026-08-22T01:00:00Z",
    }


class PullRequestApi:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.paths: list[str] = []

    def get(self, path: str) -> object:
        self.paths.append(path)
        return self.payload

    def post(self, path: str, payload: dict[str, object]) -> object:
        raise AssertionError(f"unexpected POST {path}: {payload}")

    def paginate(self, path: str) -> list[dict[str, Any]]:
        raise AssertionError(f"unexpected pagination: {path}")


def test_load_pull_request_strictly_parses_security_fields() -> None:
    api = PullRequestApi(pull_request_payload())

    pull_request = github.load_pull_request(api, REPOSITORY, PULL_REQUEST)

    assert pull_request.number == PULL_REQUEST
    assert pull_request.head_sha == HEAD
    assert pull_request.base_sha == BASE
    assert pull_request.created_at == datetime(2026, 8, 22, 1, 0, tzinfo=UTC)
    assert api.paths == [f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}"]


def test_load_pull_request_does_not_require_a_live_author_record() -> None:
    payload = pull_request_payload()
    payload.pop("user")

    assert github.load_pull_request(PullRequestApi(payload), REPOSITORY, PULL_REQUEST).number == 17


def test_load_pull_request_rejects_a_displaced_base_repository() -> None:
    payload = pull_request_payload()
    base = payload["base"]
    assert isinstance(base, dict)
    base["repo"] = {"full_name": "attacker/repository"}

    with pytest.raises(GateError, match="base repository does not match"):
        github.load_pull_request(PullRequestApi(payload), REPOSITORY, PULL_REQUEST)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("draft", "false", "draft state"),
        ("state", "merged", "state"),
        ("created_at", None, "creation"),
        ("number", PULL_REQUEST + 1, "different pull request"),
    ],
)
def test_load_pull_request_rejects_malformed_or_displaced_payloads(
    field: str, value: object, message: str
) -> None:
    payload = pull_request_payload()
    payload[field] = value
    with pytest.raises(GateError, match=message):
        github.load_pull_request(PullRequestApi(payload), REPOSITORY, PULL_REQUEST)
