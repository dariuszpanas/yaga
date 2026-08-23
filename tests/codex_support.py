"""Shared realistic GitHub payload builders for direct Codex gate tests."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from yaga.codex import constants
from yaga.errors import GateError

REPOSITORY = "dariuszpanas/django-ray"
SERVER_URL = "https://github.com"
WORKFLOW_PATH = ".github/workflows/codex-review.yml"
LIFECYCLE_WORKFLOW_PATH = ".github/workflows/review-policy.yml"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
PULL_REQUEST = 430
HEAD = "a" * 40
BASE = "b" * 40
HEAD_REPOSITORY = "contributor/django-ray"
HEAD_REF = "feat/codex-status"
BASE_REF = "main"
AUTHOR_ID = 9_876_543
AUTHOR_LOGIN = "contributor"
RUN_ID = 32_595_308_440
RUN_ATTEMPT = 1
RUN_NUMBER = 70
BOUNDARY_AT = datetime(2026, 8, 22, 18, 0, tzinfo=UTC)
OUTCOME_AT = BOUNDARY_AT + timedelta(minutes=1)
ACTIONS_USER: dict[str, object] = {
    "id": constants.GITHUB_ACTIONS_USER_ID,
    "login": constants.GITHUB_ACTIONS_LOGIN,
}
CONNECTOR_USER: dict[str, object] = {
    "id": constants.CODEX_CONNECTOR_USER_ID,
    "login": constants.CODEX_CONNECTOR_LOGIN,
}
CONNECTOR_APP: dict[str, object] = {
    "id": constants.CODEX_CONNECTOR_APP_ID,
    "slug": constants.CODEX_CONNECTOR_APP_SLUG,
}
TARGET_URL = f"{SERVER_URL}/{REPOSITORY}/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}"


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _pull_request(
    *,
    number: int = PULL_REQUEST,
    head: str = HEAD,
    base: str = BASE,
    base_ref: str = BASE_REF,
    state: str = "open",
    draft: bool = False,
    updated_at: datetime = BOUNDARY_AT,
) -> dict[str, object]:
    return {
        "number": number,
        "head": {
            "sha": head,
            "ref": HEAD_REF,
            "repo": {"full_name": HEAD_REPOSITORY},
        },
        "base": {
            "sha": base,
            "ref": base_ref,
            "repo": {"full_name": REPOSITORY},
        },
        "draft": draft,
        "state": state,
        "user": {"id": AUTHOR_ID, "login": AUTHOR_LOGIN},
        "created_at": "2026-08-22T17:00:00Z",
        "updated_at": _timestamp(updated_at),
    }


def _event(
    *,
    action: str = "opened",
    pull_request: dict[str, object] | None = None,
    base_changed: bool = False,
) -> dict[str, object]:
    pull_request = copy.deepcopy(pull_request or _pull_request())
    event: dict[str, object] = {
        "action": action,
        "number": pull_request["number"],
        "pull_request": pull_request,
        "repository": {"default_branch": BASE_REF, "full_name": REPOSITORY},
    }
    if action == "edited":
        event["changes"] = {"base": {"ref": {"from": "release"}}} if base_changed else {}
    return event


def _association(
    *,
    number: int = PULL_REQUEST,
    head: str = HEAD,
    state: str = "open",
) -> dict[str, object]:
    return {"number": number, "head": {"sha": head}, "state": state}


def _run(
    *,
    run_id: int = RUN_ID,
    run_attempt: int = RUN_ATTEMPT,
    run_number: int = RUN_NUMBER,
    head: str = HEAD,
    path: str = WORKFLOW_PATH,
    event: str = "pull_request_target",
    status: str = "completed",
    conclusion: str = "success",
    created_at: datetime = BOUNDARY_AT,
    updated_at: datetime | None = None,
    display_title: str | None = None,
    pull_request_number: int = PULL_REQUEST,
    base: str = BASE,
    base_ref: str = BASE_REF,
) -> dict[str, object]:
    return {
        "id": run_id,
        "run_attempt": run_attempt,
        "run_number": run_number,
        "path": path,
        "event": event,
        "head_sha": head,
        "repository": {"full_name": REPOSITORY},
        "status": status,
        "conclusion": conclusion,
        "created_at": _timestamp(created_at),
        "updated_at": _timestamp(updated_at or created_at + timedelta(minutes=10)),
        "display_title": display_title or f"YAGA opened boundary for #{PULL_REQUEST}",
        "head_branch": HEAD_REF,
        "head_repository": {"full_name": HEAD_REPOSITORY},
        "pull_requests": [
            {
                "number": pull_request_number,
                "head": {"sha": head},
                "base": {"sha": base, "ref": base_ref},
            }
        ],
    }


def _status(
    *,
    status_id: int = 9_001,
    state: str = "pending",
    context: str = constants.CODEX_STATUS_CONTEXT,
    description: str | None = constants.PENDING_DESCRIPTION,
    target_url: str | None = TARGET_URL,
    created_at: datetime = OUTCOME_AT,
    creator: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": status_id,
        "state": state,
        "context": context,
        "description": description,
        "target_url": target_url,
        "created_at": _timestamp(created_at),
        "creator": copy.deepcopy(creator or ACTIONS_USER),
    }


def _clean_body(commit: str = HEAD, suffix: str | None = "Hooray!") -> str:
    flourish = "" if suffix is None else f" {suffix}"
    return (
        f"Codex Review: Didn't find any major issues.{flourish}\n\n**Reviewed commit:** `{commit}`"
    )


def _clean_footer(*, current: bool = True) -> str:
    intro = (
        "[Your team has set up Codex to review pull requests in this repo]"
        "(https://chatgpt.com/codex/cloud/settings/general). Reviews are triggered when you"
        if current
        else "Codex has been enabled to automatically review pull requests in this repo. "
        "Reviews are triggered when you"
    )
    outro = (
        'Codex can also answer questions or update the PR. Try commenting "@codex address '
        'that feedback".'
        if current
        else "When you [sign up for Codex through ChatGPT](https://openai.com/codex), Codex "
        'can also answer questions or update the PR, like "@codex address that feedback".'
    )
    return (
        "\n\n<details> <summary>ℹ️ About Codex in GitHub</summary>\n<br/>\n\n"
        f"{intro}\n"
        "- Open a pull request for review\n"
        "- Mark a draft as ready\n"
        '- Comment "@codex review".\n\n'
        "If Codex has suggestions, it will comment; otherwise it will react with 👍.\n\n\n\n"
        f"{outro}\n            \n</details>"
    )


def _clean_comment(
    *,
    body: object | None = None,
    comment_id: int = 5_000,
    created_at: datetime = OUTCOME_AT,
    updated_at: datetime | None = None,
    user: dict[str, object] | None = None,
    app: object = ...,
) -> dict[str, object]:
    return {
        "id": comment_id,
        "body": _clean_body() if body is None else body,
        "created_at": _timestamp(created_at),
        "updated_at": _timestamp(updated_at or created_at),
        "user": copy.deepcopy(user or CONNECTOR_USER),
        "performed_via_github_app": copy.deepcopy(CONNECTOR_APP if app is ... else app),
    }


def _formal_body(commit: str = HEAD) -> str:
    return (
        f"{constants.FORMAL_REVIEW_PREFIX}\n\n"
        "- Consider one focused improvement.\n\n"
        f"**Reviewed commit:** `{commit}`"
    )


def _review(
    *,
    body: object | None = None,
    review_id: int = 6_000,
    state: str = "COMMENTED",
    commit_id: str = HEAD,
    submitted_at: datetime = OUTCOME_AT,
    user: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": review_id,
        "body": _formal_body() if body is None else body,
        "state": state,
        "commit_id": commit_id,
        "submitted_at": _timestamp(submitted_at),
        "user": copy.deepcopy(user or CONNECTOR_USER),
    }


def _reaction(
    *,
    content: object = "+1",
    reaction_id: int = 7_000,
    created_at: datetime = OUTCOME_AT,
    user: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": reaction_id,
        "content": content,
        "created_at": _timestamp(created_at),
        "user": copy.deepcopy(user or CONNECTOR_USER),
    }


class FakeApi:
    def __init__(
        self,
        *,
        pull_request: dict[str, object] | None = None,
        associations: list[dict[str, object]] | None = None,
        comments: list[dict[str, object]] | None = None,
        reviews: list[dict[str, object]] | None = None,
        reactions: list[dict[str, object]] | None = None,
        statuses: list[dict[str, object]] | None = None,
        runs: list[dict[str, object]] | None = None,
        current_run: dict[str, object] | None = None,
        default_branch: str = BASE_REF,
    ) -> None:
        initial_pull_request = copy.deepcopy(pull_request or _pull_request())
        self.pull_requests = {cast(int, initial_pull_request["number"]): initial_pull_request}
        self.associations = copy.deepcopy(associations or [_association()])
        self.comments = copy.deepcopy(comments or [])
        self.reviews = copy.deepcopy(reviews or [])
        self.reactions = copy.deepcopy(reactions or [])
        self.statuses = copy.deepcopy(statuses or [])
        self.current_run = copy.deepcopy(current_run or _run())
        self.runs = copy.deepcopy(runs or [self.current_run])
        self.default_branch = default_branch
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.reads: list[str] = []
        self.success_tail_calls: list[tuple[int, int]] = []

    def get(self, path: str) -> object:
        self.reads.append(path)
        if path == f"/repos/{REPOSITORY}":
            return {"default_branch": self.default_branch, "full_name": REPOSITORY}
        actions_prefix = f"/repos/{REPOSITORY}/actions/runs/"
        if path.startswith(actions_prefix):
            run_id = int(path.removeprefix(actions_prefix))
            for run in [self.current_run, *self.runs]:
                if run.get("id") == run_id:
                    return copy.deepcopy(run)
            raise AssertionError(f"unknown workflow run {run_id}")
        workflows_prefix = f"/repos/{REPOSITORY}/actions/workflows/"
        if path.startswith(workflows_prefix) and "/runs?" in path:
            filename = path.removeprefix(workflows_prefix).split("/runs?", 1)[0]
            matches = [
                run for run in self.runs if str(run.get("path", "")).rsplit("/", 1)[-1] == filename
            ]
            return {"total_count": len(matches), "workflow_runs": copy.deepcopy(matches)}
        issue_comment_prefix = f"/repos/{REPOSITORY}/issues/comments/"
        if path.startswith(issue_comment_prefix):
            comment_id = int(path.removeprefix(issue_comment_prefix))
            for item in self.comments:
                if item.get("id") == comment_id:
                    return copy.deepcopy(item)
            raise GateError("GitHub API GET failed with HTTP 404")
        review_prefix = f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}/reviews/"
        if path.startswith(review_prefix):
            review_id = int(path.removeprefix(review_prefix))
            for item in self.reviews:
                if item.get("id") == review_id:
                    return copy.deepcopy(item)
            raise GateError("GitHub API GET failed with HTTP 404")
        pull_prefix = f"/repos/{REPOSITORY}/pulls/"
        if path.startswith(pull_prefix) and "?" not in path:
            number = int(path.removeprefix(pull_prefix))
            return copy.deepcopy(self.pull_requests[number])
        if path == (
            f"/repos/{REPOSITORY}/commits/{HEAD}/pulls"
            f"?per_page={constants.MAX_HEAD_ASSOCIATIONS}&page=1"
        ):
            return copy.deepcopy(self.associations)
        statuses_prefix = (
            f"/repos/{REPOSITORY}/commits/{HEAD}/statuses"
            f"?per_page={constants.MAX_STATUS_PAGE_RECORDS}&page="
        )
        if path.startswith(statuses_prefix):
            page = int(path.removeprefix(statuses_prefix))
            start = (page - 1) * constants.MAX_STATUS_PAGE_RECORDS
            ordered = list(reversed(self.statuses))
            return copy.deepcopy(ordered[start : start + constants.MAX_STATUS_PAGE_RECORDS])
        commit_prefix = f"/repos/{REPOSITORY}/commits/"
        if path.startswith(commit_prefix) and "/" not in path.removeprefix(commit_prefix):
            prefix = path.removeprefix(commit_prefix)
            return {"sha": HEAD if HEAD.startswith(prefix) else "f" * 40}
        raise AssertionError(f"unexpected GET {path}")

    def paginate(self, path: str) -> list[dict[str, Any]]:
        self.reads.append(path)
        if path == f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/comments":
            return copy.deepcopy(self.comments)
        if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}/reviews":
            return copy.deepcopy(self.reviews)
        if path == f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/reactions":
            return copy.deepcopy(self.reactions)
        raise AssertionError(f"unexpected pagination {path}")

    def post(self, path: str, payload: dict[str, object]) -> object:
        comment_path = f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/comments"
        if path == comment_path:
            self.posts.append((path, copy.deepcopy(payload)))
            comment = {
                "id": 20_000 + len(self.posts),
                "body": payload.get("body"),
                "user": copy.deepcopy(ACTIONS_USER),
                "performed_via_github_app": {"id": 15_368, "slug": "github-actions"},
                "created_at": _timestamp(BOUNDARY_AT + timedelta(hours=2)),
                "updated_at": _timestamp(BOUNDARY_AT + timedelta(hours=2)),
            }
            self.comments.append(comment)
            return copy.deepcopy(comment)
        prefix = f"/repos/{REPOSITORY}/statuses/"
        if not path.startswith(prefix):
            raise AssertionError(f"publisher attempted a non-status write: {path}")
        self.posts.append((path, copy.deepcopy(payload)))
        status = _status(
            status_id=10_000 + len(self.posts),
            state=str(payload["state"]),
            context=str(payload["context"]),
            description=str(payload["description"]),
            target_url=str(payload["target_url"]),
            created_at=BOUNDARY_AT + timedelta(hours=2, seconds=len(self.posts)),
        )
        self.statuses.append(status)
        return copy.deepcopy(status)

    def require_success_tail(self, count: int, *, cleanup_margin_seconds: int) -> None:
        self.success_tail_calls.append((count, cleanup_margin_seconds))
