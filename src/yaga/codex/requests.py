"""Publish exact-bound, idempotent Codex authorization comments."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from yaga.codex.constants import GITHUB_ACTIONS_LOGIN, GITHUB_ACTIONS_USER_ID
from yaga.errors import GateError
from yaga.github import MAX_API_RECORDS, RestApi
from yaga.models import actor_is, commit_sha, positive_int, record, repository_name, timestamp

GITHUB_ACTIONS_APP_ID = 15_368
GITHUB_ACTIONS_APP_SLUG = "github-actions"
REQUEST_LITERAL = "@codex review"
APPROVAL_LITERAL = "YAGA recorded maintainer approval for this exact Codex result."
AUTHORIZATION_MARKER = "yaga-codex-authorization:v2"
AUTHORIZATION_KINDS = frozenset({"approval", "request"})
MAX_REQUEST_BODY_BYTES = 1_024

__all__ = [
    "AuthorizationComments",
    "RequestComment",
    "RequestKey",
    "ensure_authorization",
    "find_authorization",
    "validate_authorization",
]

_AUTHORIZATION_BODY_RE = re.compile(
    r"\A(?P<literal>@codex review|" + re.escape(APPROVAL_LITERAL) + r")\n\n"
    r"<!-- yaga-codex-authorization:v2 "
    r"kind=(?P<kind>approval|request) "
    r"repository=(?P<repository>[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}) "
    r"pull-request=(?P<pull_request>[1-9][0-9]{0,18}) "
    r"head-sha=(?P<head_sha>[0-9a-f]{40}) "
    r"base-sha=(?P<base_sha>[0-9a-f]{40}) "
    r"boundary-run-id=(?P<boundary_run_id>[1-9][0-9]{0,18}) "
    r"source-run-id=(?P<source_run_id>[1-9][0-9]{0,18}) "
    r"source-run-attempt=(?P<source_run_attempt>[1-9][0-9]{0,18}) -->\Z"
)


def _authorization_kind(value: object) -> str:
    if not isinstance(value, str) or value not in AUTHORIZATION_KINDS:
        raise GateError("Codex authorization kind is invalid")
    return value


@dataclass(frozen=True)
class RequestKey:
    """Exact lifecycle candidate authorized to receive one review request."""

    repository: str
    pull_request_number: int
    head_sha: str
    base_sha: str
    boundary_run_id: int
    source_run_id: int
    source_run_attempt: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", repository_name(self.repository))
        object.__setattr__(
            self,
            "pull_request_number",
            positive_int(self.pull_request_number, "Codex request pull request"),
        )
        object.__setattr__(
            self,
            "head_sha",
            commit_sha(self.head_sha, "Codex request head"),
        )
        object.__setattr__(
            self,
            "base_sha",
            commit_sha(self.base_sha, "Codex request base"),
        )
        object.__setattr__(
            self,
            "boundary_run_id",
            positive_int(self.boundary_run_id, "Codex request boundary run"),
        )
        object.__setattr__(
            self,
            "source_run_id",
            positive_int(self.source_run_id, "Codex request source run"),
        )
        object.__setattr__(
            self,
            "source_run_attempt",
            positive_int(self.source_run_attempt, "Codex request source run attempt"),
        )

    def body_for(self, kind: str) -> str:
        """Render one canonical request or non-triggering approval marker."""
        kind = _authorization_kind(kind)
        literal = REQUEST_LITERAL if kind == "request" else APPROVAL_LITERAL
        return (
            f"{literal}\n\n"
            f"<!-- {AUTHORIZATION_MARKER} kind={kind} repository={self.repository} "
            f"pull-request={self.pull_request_number} head-sha={self.head_sha} "
            f"base-sha={self.base_sha} boundary-run-id={self.boundary_run_id} "
            f"source-run-id={self.source_run_id} "
            f"source-run-attempt={self.source_run_attempt} -->"
        )

    @property
    def body(self) -> str:
        """Render the quota-consuming request form for callers that need it."""
        return self.body_for("request")

    @property
    def approval_body(self) -> str:
        """Render the non-triggering protected-environment authorization form."""
        return self.body_for("approval")


@dataclass(frozen=True)
class RequestComment:
    """One exact GitHub Actions-authored authorization comment."""

    comment_id: int
    key: RequestKey
    kind: str = "request"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "comment_id",
            positive_int(self.comment_id, "Codex authorization comment ID"),
        )
        if not isinstance(self.key, RequestKey):
            raise GateError("Codex authorization comment key is invalid")
        object.__setattr__(self, "kind", _authorization_kind(self.kind))
        if self.created_at is None or self.updated_at is None:
            raise GateError("Codex authorization comment timestamps are missing")
        if self.updated_at < self.created_at:
            raise GateError("Codex authorization comment update predates its creation")


@dataclass(frozen=True)
class AuthorizationComments:
    """The at-most-one marker of each kind for one lifecycle boundary."""

    request: RequestComment | None = None
    approval: RequestComment | None = None

    @property
    def any(self) -> RequestComment | None:
        """Prefer the quota-consuming request as the authorization capability."""
        return self.request or self.approval


def _bounded_body(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > MAX_REQUEST_BODY_BYTES:
        return None
    return value


def _parse_authorization_body(value: object) -> tuple[RequestKey, str] | None:
    body = _bounded_body(value)
    if body is None:
        return None
    match = _AUTHORIZATION_BODY_RE.fullmatch(body)
    if match is None:
        return None
    kind = _authorization_kind(match.group("kind"))
    expected_literal = REQUEST_LITERAL if kind == "request" else APPROVAL_LITERAL
    if match.group("literal") != expected_literal:
        return None
    return (
        RequestKey(
            repository=match.group("repository"),
            pull_request_number=int(match.group("pull_request")),
            head_sha=match.group("head_sha"),
            base_sha=match.group("base_sha"),
            boundary_run_id=int(match.group("boundary_run_id")),
            source_run_id=int(match.group("source_run_id")),
            source_run_attempt=int(match.group("source_run_attempt")),
        ),
        kind,
    )


def _looks_like_authorization(value: object) -> bool:
    return isinstance(value, str) and (
        value.startswith((REQUEST_LITERAL, APPROVAL_LITERAL)) or AUTHORIZATION_MARKER in value
    )


def _authorization_comment(
    value: object,
    *,
    label: str,
) -> tuple[int, RequestComment | None]:
    payload = record(value, label)
    comment_id = positive_int(payload.get("id"), f"{label} ID")
    if not actor_is(
        payload,
        user_id=GITHUB_ACTIONS_USER_ID,
        user_login=GITHUB_ACTIONS_LOGIN,
    ):
        return comment_id, None
    app = payload.get("performed_via_github_app")
    if not isinstance(app, dict) or (
        app.get("id") != GITHUB_ACTIONS_APP_ID or app.get("slug") != GITHUB_ACTIONS_APP_SLUG
    ):
        return comment_id, None
    parsed = _parse_authorization_body(payload.get("body"))
    if parsed is None:
        if _looks_like_authorization(payload.get("body")):
            raise GateError(f"{label} has a malformed YAGA Codex authorization")
        return comment_id, None
    key, kind = parsed
    return comment_id, RequestComment(
        comment_id,
        key,
        kind,
        timestamp(payload.get("created_at"), f"{label} creation"),
        timestamp(payload.get("updated_at"), f"{label} update"),
    )


def _request_path(key: RequestKey) -> str:
    return f"/repos/{key.repository}/issues/{key.pull_request_number}/comments"


def _same_boundary(left: RequestKey, right: RequestKey) -> bool:
    return (
        left.repository,
        left.pull_request_number,
        left.head_sha,
        left.base_sha,
        left.boundary_run_id,
    ) == (
        right.repository,
        right.pull_request_number,
        right.head_sha,
        right.base_sha,
        right.boundary_run_id,
    )


def find_authorization(api: RestApi, *, key: RequestKey) -> AuthorizationComments:
    """Find each bound marker in one complete bounded comment history."""
    if not isinstance(key, RequestKey):
        raise GateError("Codex request key is invalid")
    payload = api.paginate(_request_path(key))
    if not isinstance(payload, list) or len(payload) >= MAX_API_RECORDS:
        raise GateError("Codex authorization comment history is truncated or invalid")
    seen_ids: set[int] = set()
    matches: dict[str, RequestComment] = {}
    for index, item in enumerate(payload):
        comment_id, comment = _authorization_comment(
            item,
            label=f"Codex authorization comment {index}",
        )
        if comment_id in seen_ids:
            raise GateError("Codex authorization comment history repeats a comment")
        seen_ids.add(comment_id)
        # A CI rerun may carry a newer source run attempt, but the lifecycle
        # candidate is still entitled to at most one marker of each kind.
        if comment is None or not _same_boundary(comment.key, key):
            continue
        if comment.kind in matches:
            raise GateError(f"multiple exact Codex {comment.kind} markers exist for this boundary")
        matches[comment.kind] = comment
    return AuthorizationComments(
        request=matches.get("request"),
        approval=matches.get("approval"),
    )


def ensure_authorization(
    api: RestApi,
    *,
    key: RequestKey,
    kind: str,
    before_post: Callable[[], bool] | None = None,
) -> RequestComment:
    """Create one marker of the requested kind, with one confirmed post."""
    if not isinstance(key, RequestKey):
        raise GateError("Codex request key is invalid")
    kind = _authorization_kind(kind)

    def matching(comments: AuthorizationComments) -> RequestComment | None:
        return comments.request if kind == "request" else comments.approval

    existing = matching(find_authorization(api, key=key))
    if existing is not None:
        return existing

    body = key.body_for(kind)
    # The publisher workflow serializes a head without canceling an in-flight
    # run. The second list closes the ordinary check/write window inside that
    # server-side workflow mutex. The POST is never retried.
    existing = matching(find_authorization(api, key=key))
    if existing is not None:
        return existing
    if before_post is not None and not before_post():
        raise GateError("Codex authorization candidate changed immediately before publication")

    response_comment: RequestComment | None = None
    response_error: GateError | None = None
    try:
        _, response_comment = _authorization_comment(
            api.post(_request_path(key), {"body": body}),
            label="published Codex authorization comment",
        )
        if response_comment is None or response_comment.key != key or response_comment.kind != kind:
            raise GateError("GitHub returned a different Codex authorization comment")
    except GateError as error:
        response_error = error

    confirmed = matching(find_authorization(api, key=key))
    if confirmed is not None:
        if response_comment is not None and confirmed.comment_id != response_comment.comment_id:
            raise GateError("GitHub returned an inconsistent Codex authorization comment")
        return confirmed
    if response_error is not None:
        raise response_error
    raise GateError("published Codex authorization comment is not visible after creation")


def validate_authorization(
    api: RestApi,
    *,
    key: RequestKey,
    comment: RequestComment,
) -> bool:
    """Revalidate one exact marker without rescanning the comment history."""
    if not isinstance(key, RequestKey) or not isinstance(comment, RequestComment):
        raise GateError("Codex authorization capability is invalid")
    _, current = _authorization_comment(
        api.get(f"/repos/{key.repository}/issues/comments/{comment.comment_id}"),
        label="current Codex authorization comment",
    )
    return bool(current == comment and _same_boundary(comment.key, key))
