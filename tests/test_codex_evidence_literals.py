"""Literal connector payload fixtures for provenance-sensitive evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from yaga.codex import evidence
from yaga.codex.constants import FORMAL_REVIEW_PREFIX
from yaga.models import PullRequest

REPOSITORY = "owner/repository"
HEAD = "a" * 40
BASE = "b" * 40
BOUNDARY = datetime(2026, 8, 22, 18, 0, tzinfo=UTC)
OUTCOME = BOUNDARY + timedelta(minutes=1)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _pull_request() -> PullRequest:
    return PullRequest(
        number=7,
        head_sha=HEAD,
        head_ref="feat/yaga",
        head_repository="contributor/repository",
        base_ref="main",
        base_sha=BASE,
        draft=False,
        state="open",
        created_at=BOUNDARY,
    )


class LiteralEvidenceApi:
    def __init__(
        self,
        *,
        comments: list[dict[str, Any]] | None = None,
        reviews: list[dict[str, Any]] | None = None,
        reactions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.comments = comments or []
        self.reviews = reviews or []
        self.reactions = reactions or []

    def get(self, path: str) -> object:
        prefix = f"/repos/{REPOSITORY}/commits/"
        if path.startswith(prefix):
            assert HEAD.startswith(path.removeprefix(prefix))
            return {"sha": HEAD}
        raise AssertionError(f"unexpected GET: {path}")

    def paginate(self, path: str) -> list[dict[str, Any]]:
        if path == f"/repos/{REPOSITORY}/issues/7/comments":
            return self.comments
        if path == f"/repos/{REPOSITORY}/pulls/7/reviews":
            return self.reviews
        if path == f"/repos/{REPOSITORY}/issues/7/reactions":
            return self.reactions
        raise AssertionError(f"unexpected pagination: {path}")

    def post(self, path: str, payload: dict[str, object]) -> object:
        raise AssertionError(f"evidence parsing attempted a write: {path} {payload}")


def test_literal_connector_issue_comment_requires_the_official_app() -> None:
    comment = {
        "id": 9_100_001,
        "body": (
            f"Agent Review: Didn't find any major issues. Hooray!\n\n**Reviewed commit:** `{HEAD}`"
        ),
        "created_at": _stamp(OUTCOME),
        "updated_at": _stamp(OUTCOME),
        "user": {
            "id": 199_175_422,
            "login": "chatgpt-codex-connector[bot]",
            "type": "Bot",
        },
        "performed_via_github_app": {
            "id": 1_144_995,
            "slug": "chatgpt-codex-connector",
        },
    }
    result = evidence.check_codex_outcome_policy(
        LiteralEvidenceApi(comments=[comment]),
        repository=REPOSITORY,
        pull_request=_pull_request(),
        not_before=BOUNDARY,
        allow_clean_reaction=False,
    )
    assert result == "trusted exact-head Codex clean comment is current"


def test_literal_formal_review_accepts_a_null_performing_app() -> None:
    review = {
        "id": 9_100_002,
        "body": (
            f"{FORMAL_REVIEW_PREFIX}\n\n"
            "- Consider one focused improvement.\n\n"
            f"**Reviewed commit:** `{HEAD}`"
        ),
        "state": "COMMENTED",
        "commit_id": HEAD,
        "submitted_at": _stamp(OUTCOME),
        "user": {
            "id": 199_175_422,
            "login": "chatgpt-codex-connector[bot]",
            "type": "Bot",
        },
        "performed_via_github_app": None,
    }
    result = evidence.check_codex_outcome_policy(
        LiteralEvidenceApi(reviews=[review]),
        repository=REPOSITORY,
        pull_request=_pull_request(),
        not_before=BOUNDARY,
        allow_clean_reaction=False,
    )
    assert result == "trusted exact-head Codex findings review is current"


def test_literal_initial_reaction_accepts_the_connector_user_actor_type() -> None:
    reaction = {
        "id": 9_100_003,
        "content": "+1",
        "created_at": _stamp(OUTCOME),
        "user": {
            "id": 199_175_422,
            "login": "chatgpt-codex-connector[bot]",
            "type": "User",
        },
    }
    result = evidence.check_codex_outcome_policy(
        LiteralEvidenceApi(reactions=[reaction]),
        repository=REPOSITORY,
        pull_request=_pull_request(),
        not_before=BOUNDARY,
        allow_clean_reaction=True,
    )
    assert result == "trusted exact-head Codex clean automatic reaction is current"
