"""Tests for exact-bound, idempotent Codex review request comments."""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from yaga.codex.constants import MAX_PRIOR_REQUESTS
from yaga.codex.requests import (
    AuthorizationComments,
    RequestComment,
    RequestKey,
    ensure_authorization,
    find_authorization,
    validate_authorization,
)
from yaga.errors import GateError
from yaga.github import MAX_API_RECORDS

REPOSITORY = "example/widgets"
PULL_REQUEST = 42
HEAD = "a" * 40
BASE = "b" * 40
BOUNDARY_RUN_ID = 9_876_543_210
SOURCE_RUN_ID = 8_765_432_109
SOURCE_RUN_ATTEMPT = 3
REQUEST_PATH = f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/comments"

ACTIONS_USER = {"id": 41_898_282, "login": "github-actions[bot]"}
ACTIONS_APP = {"id": 15_368, "slug": "github-actions"}
CONTRIBUTOR_USER = {"id": 123_456, "login": "contributor"}
AUTHORIZATION_AT = datetime(2026, 8, 22, 18, 0, tzinfo=UTC)


def _key(**overrides: object) -> RequestKey:
    values: dict[str, Any] = {
        "repository": REPOSITORY,
        "pull_request_number": PULL_REQUEST,
        "head_sha": HEAD,
        "base_sha": BASE,
        "boundary_run_id": BOUNDARY_RUN_ID,
        "source_run_id": SOURCE_RUN_ID,
        "source_run_attempt": SOURCE_RUN_ATTEMPT,
    }
    values.update(overrides)
    return RequestKey(**values)


def _comment(
    *,
    comment_id: int = 7_001,
    key: RequestKey | None = None,
    body: object | None = None,
    user: object = ACTIONS_USER,
    app: object = ACTIONS_APP,
    kind: str = "request",
    created_at: datetime = AUTHORIZATION_AT,
    updated_at: datetime | None = None,
) -> dict[str, object]:
    if body is None:
        body = (key or _key()).body_for(kind)
    return {
        "id": comment_id,
        "body": body,
        "user": copy.deepcopy(user),
        "performed_via_github_app": copy.deepcopy(app),
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "updated_at": (updated_at or created_at).isoformat().replace("+00:00", "Z"),
    }


def _parsed_comment(
    comment_id: int,
    key: RequestKey,
    kind: str = "request",
) -> RequestComment:
    return RequestComment(comment_id, key, kind, AUTHORIZATION_AT, AUTHORIZATION_AT)


class FakeRequestApi:
    def __init__(
        self,
        comments: list[dict[str, object]] | None = None,
        *,
        paginate_hook: Callable[[FakeRequestApi], object] | None = None,
        post_hook: Callable[[FakeRequestApi, str, dict[str, object]], object] | None = None,
    ) -> None:
        self.comments = copy.deepcopy(comments or [])
        self.paginate_hook = paginate_hook
        self.post_hook = post_hook
        self.paginate_calls: list[str] = []
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.next_id = 10_001

    def get(self, path: str) -> object:
        prefix = f"/repos/{REPOSITORY}/issues/comments/"
        assert path.startswith(prefix)
        comment_id = int(path.removeprefix(prefix))
        return copy.deepcopy(next(item for item in self.comments if item.get("id") == comment_id))

    def paginate(self, path: str) -> Any:
        assert path == REQUEST_PATH
        self.paginate_calls.append(path)
        if self.paginate_hook is not None:
            return self.paginate_hook(self)
        return copy.deepcopy(self.comments)

    def post(self, path: str, payload: dict[str, object]) -> object:
        assert path == REQUEST_PATH
        self.posts.append((path, copy.deepcopy(payload)))
        if self.post_hook is not None:
            return self.post_hook(self, path, payload)
        created = _comment(comment_id=self.next_id, body=payload.get("body"))
        self.next_id += 1
        self.comments.append(created)
        return copy.deepcopy(created)


def test_request_body_is_one_deterministic_closed_protocol() -> None:
    key = _key()

    assert key.body == (
        "@codex review\n\n"
        "<!-- yaga-codex-authorization:v2 kind=request repository=example/widgets "
        "pull-request=42 "
        f"head-sha={HEAD} base-sha={BASE} boundary-run-id=9876543210 "
        "source-run-id=8765432109 source-run-attempt=3 -->"
    )
    assert find_authorization(FakeRequestApi([_comment(key=key)]), key=key).request == (
        _parsed_comment(7_001, key)
    )


def test_approval_marker_is_non_triggering_but_equally_bound() -> None:
    key = _key()
    comment = _comment(comment_id=7_002, key=key, kind="approval")

    assert key.approval_body == (
        "YAGA recorded maintainer approval for this exact Codex result.\n\n"
        "<!-- yaga-codex-authorization:v2 kind=approval repository=example/widgets "
        "pull-request=42 "
        f"head-sha={HEAD} base-sha={BASE} boundary-run-id=9876543210 "
        "source-run-id=8765432109 source-run-attempt=3 -->"
    )
    assert "@codex" not in key.approval_body
    assert find_authorization(FakeRequestApi([comment]), key=key) == AuthorizationComments(
        approval=_parsed_comment(7_002, key, "approval")
    )


def test_exact_authorization_capability_detects_body_edits() -> None:
    key = _key()
    comment = _comment(comment_id=7_003, key=key, kind="approval")
    api = FakeRequestApi([comment])
    capability = find_authorization(api, key=key).approval
    assert capability is not None
    assert validate_authorization(api, key=key, comment=capability)

    api.comments[0]["body"] = "ordinary comment"
    assert not validate_authorization(api, key=key, comment=capability)


def test_request_and_approval_are_independently_idempotent() -> None:
    key = _key()
    api = FakeRequestApi([_comment(comment_id=1, key=key, kind="approval")])

    request = ensure_authorization(api, key=key, kind="request")

    assert request.kind == "request"
    assert len(api.posts) == 1
    state = find_authorization(api, key=key)
    assert state.approval == _parsed_comment(1, key, "approval")
    assert state.request == request


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "unsafe"),
        ("pull_request_number", 0),
        ("pull_request_number", True),
        ("head_sha", "a" * 39),
        ("head_sha", "A" * 40),
        ("base_sha", "b" * 39),
        ("boundary_run_id", 0),
        ("source_run_id", 0),
        ("source_run_attempt", 0),
    ],
)
def test_request_key_rejects_partial_or_unsafe_identity(field: str, value: object) -> None:
    with pytest.raises(GateError):
        _key(**{field: value})


@pytest.mark.parametrize(
    "body",
    [
        _key().body + "\n",
        _key().body.replace("@codex review", "@Codex review"),
        _key().body.replace("\n\n", "\n"),
        _key().body.replace(f"head-sha={HEAD}", f"head-sha={'a' * 39}"),
        _key().body.replace(" boundary-run-id=", " extra=yes boundary-run-id="),
        _key().body.replace(" source-run-id=", " source-run="),
        _key().body.replace("source-run-attempt=3", "source-run-attempt=0"),
        "@codex review",
        f"prefix <!-- yaga-codex-authorization:v2 repository={REPOSITORY} -->",
    ],
)
def test_trusted_request_body_parser_is_closed(body: str) -> None:
    with pytest.raises(GateError, match="malformed"):
        find_authorization(FakeRequestApi([_comment(body=body)]), key=_key())


def test_contributor_wrong_app_and_stale_requests_are_ignored() -> None:
    stale = _key(head_sha="c" * 40)
    comments = [
        _comment(comment_id=1, user=CONTRIBUTOR_USER),
        _comment(comment_id=2, app={"id": 15_369, "slug": "github-actions"}),
        _comment(comment_id=3, app={"id": 15_368, "slug": "lookalike"}),
        _comment(comment_id=4, key=stale),
        _comment(comment_id=5, body="ordinary Actions automation comment"),
        _comment(comment_id=6),
    ]

    assert find_authorization(FakeRequestApi(comments), key=_key()).request == _parsed_comment(
        6, _key()
    )


@pytest.mark.parametrize(
    "stale_identity",
    [
        {"repository": "example/other"},
        {"pull_request_number": PULL_REQUEST + 1},
        {"head_sha": "c" * 40},
        {"base_sha": "d" * 40},
        {"boundary_run_id": BOUNDARY_RUN_ID + 1},
    ],
)
def test_every_nonmatching_binding_is_stale(stale_identity: dict[str, object]) -> None:
    assert (
        find_authorization(
            FakeRequestApi([_comment(key=_key(**stale_identity))]), key=_key()
        ).request
        is None
    )


def test_prior_request_history_is_exposed_for_the_same_pull_request() -> None:
    prior = _key(head_sha="c" * 40, boundary_run_id=BOUNDARY_RUN_ID - 1)
    state = find_authorization(
        FakeRequestApi([_comment(comment_id=71, key=prior), _comment(comment_id=72)]),
        key=_key(),
    )

    assert state.request == _parsed_comment(72, _key())
    assert state.prior_requests == (_parsed_comment(71, prior),)


def test_prior_request_history_is_bounded_and_unique_per_boundary() -> None:
    duplicate = _key(head_sha="c" * 40, boundary_run_id=BOUNDARY_RUN_ID - 1)
    with pytest.raises(GateError, match="multiple exact prior"):
        find_authorization(
            FakeRequestApi(
                [
                    _comment(comment_id=81, key=duplicate),
                    _comment(comment_id=82, key=duplicate),
                ]
            ),
            key=_key(),
        )

    comments = [
        _comment(
            comment_id=90 + index,
            key=_key(
                head_sha=f"{index + 1:040x}",
                boundary_run_id=BOUNDARY_RUN_ID + index + 1,
            ),
        )
        for index in range(MAX_PRIOR_REQUESTS + 1)
    ]
    with pytest.raises(GateError, match="bounded limit"):
        find_authorization(FakeRequestApi(comments), key=_key())


def test_duplicate_exact_requests_are_ambiguous() -> None:
    api = FakeRequestApi([_comment(comment_id=1), _comment(comment_id=2)])

    with pytest.raises(GateError, match="multiple exact"):
        find_authorization(api, key=_key())


def test_source_ci_rerun_reuses_the_one_boundary_request() -> None:
    original = _key()
    rerun = _key(source_run_id=SOURCE_RUN_ID + 1, source_run_attempt=1)
    assert find_authorization(FakeRequestApi([_comment(key=original)]), key=rerun).request == (
        _parsed_comment(7_001, original)
    )


def test_comment_history_must_be_complete_bounded_and_structurally_certain() -> None:
    unrelated = [
        _comment(comment_id=index + 1, body=f"ordinary comment {index}", user=CONTRIBUTOR_USER)
        for index in range(MAX_API_RECORDS - 1)
    ]
    assert find_authorization(FakeRequestApi(unrelated), key=_key()).request is None

    with pytest.raises(GateError, match="truncated"):
        find_authorization(
            FakeRequestApi(unrelated + [_comment(comment_id=MAX_API_RECORDS)]), key=_key()
        )
    with pytest.raises(GateError, match="must be an object"):
        find_authorization(FakeRequestApi(paginate_hook=lambda _api: [None]), key=_key())
    with pytest.raises(GateError, match="repeats"):
        find_authorization(
            FakeRequestApi(
                [
                    _comment(comment_id=1, user=CONTRIBUTOR_USER),
                    _comment(comment_id=1, body="other", user=CONTRIBUTOR_USER),
                ]
            ),
            key=_key(),
        )


def test_existing_exact_request_is_idempotent_without_post() -> None:
    existing = _comment(comment_id=88)
    api = FakeRequestApi([existing])

    assert ensure_authorization(api, key=_key(), kind="request") == _parsed_comment(88, _key())
    assert len(api.paginate_calls) == 1
    assert api.posts == []


def test_second_pre_post_list_closes_an_ordinary_race() -> None:
    def insert_on_second_list(api: FakeRequestApi) -> object:
        if len(api.paginate_calls) == 2:
            api.comments.append(_comment(comment_id=89))
        return copy.deepcopy(api.comments)

    api = FakeRequestApi(paginate_hook=insert_on_second_list)

    assert ensure_authorization(api, key=_key(), kind="request") == _parsed_comment(89, _key())
    assert len(api.paginate_calls) == 2
    assert api.posts == []


def test_new_request_is_posted_once_and_confirmed_by_relist() -> None:
    api = FakeRequestApi()

    assert ensure_authorization(api, key=_key(), kind="request") == _parsed_comment(10_001, _key())
    assert len(api.paginate_calls) == 3
    assert api.posts == [(REQUEST_PATH, {"body": _key().body})]


def test_final_live_callback_can_stop_the_single_post_after_bounded_discovery() -> None:
    api = FakeRequestApi()

    with pytest.raises(GateError, match="immediately before"):
        ensure_authorization(api, key=_key(), kind="request", before_post=lambda: False)

    assert len(api.paginate_calls) == 2
    assert api.posts == []


@pytest.mark.parametrize("response_mode", ["lost", "malformed"])
def test_accepted_then_uncertain_response_recovers_only_from_one_exact_match(
    response_mode: str,
) -> None:
    def accepted_then_uncertain(
        api: FakeRequestApi,
        _path: str,
        payload: dict[str, object],
    ) -> object:
        api.comments.append(_comment(comment_id=101, body=payload.get("body")))
        if response_mode == "lost":
            raise GateError("response was lost")
        return {"id": 101, "body": "malformed"}

    api = FakeRequestApi(post_hook=accepted_then_uncertain)

    assert ensure_authorization(api, key=_key(), kind="request") == _parsed_comment(101, _key())
    assert len(api.posts) == 1
    assert len(api.paginate_calls) == 3


def test_failed_post_is_not_blindly_retried_without_a_confirmed_match() -> None:
    def fail_before_acceptance(
        _api: FakeRequestApi,
        _path: str,
        _payload: dict[str, object],
    ) -> object:
        raise GateError("POST failed")

    api = FakeRequestApi(post_hook=fail_before_acceptance)

    with pytest.raises(GateError, match="POST failed"):
        ensure_authorization(api, key=_key(), kind="request")
    assert len(api.posts) == 1
    assert len(api.paginate_calls) == 3


def test_post_race_with_duplicate_exact_requests_fails_ambiguous_without_retry() -> None:
    def create_duplicates(
        api: FakeRequestApi,
        _path: str,
        payload: dict[str, object],
    ) -> object:
        first = _comment(comment_id=201, body=payload.get("body"))
        api.comments.extend([first, _comment(comment_id=202, body=payload.get("body"))])
        return copy.deepcopy(first)

    api = FakeRequestApi(post_hook=create_duplicates)

    with pytest.raises(GateError, match="multiple exact"):
        ensure_authorization(api, key=_key(), kind="request")
    assert len(api.posts) == 1
    assert len(api.paginate_calls) == 3


def test_valid_post_response_without_visible_comment_fails_closed() -> None:
    def invisible_response(
        _api: FakeRequestApi,
        _path: str,
        payload: dict[str, object],
    ) -> object:
        return _comment(comment_id=301, body=payload.get("body"))

    api = FakeRequestApi(post_hook=invisible_response)

    with pytest.raises(GateError, match="not visible"):
        ensure_authorization(api, key=_key(), kind="request")
    assert len(api.posts) == 1


def test_uncertain_post_list_fails_closed_without_retry() -> None:
    def truncate_after_post(api: FakeRequestApi) -> object:
        if api.posts:
            return [_comment(comment_id=index + 1, user=CONTRIBUTOR_USER) for index in range(1_000)]
        return []

    api = FakeRequestApi(paginate_hook=truncate_after_post)

    with pytest.raises(GateError, match="truncated"):
        ensure_authorization(api, key=_key(), kind="request")
    assert len(api.posts) == 1
    assert len(api.paginate_calls) == 3
