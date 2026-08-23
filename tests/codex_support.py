"""Shared realistic GitHub payload builders for Codex gate tests."""

from __future__ import annotations

import copy
import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

from yaga.codex import constants, provenance, publisher, statuses
from yaga.codex.models import Candidate

REPOSITORY = "dariuszpanas/django-ray"
SERVER_URL = "https://github.com"
PULL_REQUEST = 430
HEAD = "a" * 40
BASE = "b" * 40
HEAD_REPOSITORY = "contributor/django-ray"
HEAD_REF = "feat/codex-status"
BASE_REF = "main"
RUN_ID = 32_595_308_440
BOUNDARY_AT = datetime(2026, 8, 22, 18, 0, tzinfo=UTC)
OUTCOME_AT = BOUNDARY_AT + timedelta(minutes=1)
ACTIONS_USER = {
    "id": constants.GITHUB_ACTIONS_USER_ID,
    "login": constants.GITHUB_ACTIONS_LOGIN,
}
CONNECTOR_USER = {
    "id": constants.CODEX_CONNECTOR_USER_ID,
    "login": constants.CODEX_CONNECTOR_LOGIN,
}
CONNECTOR_APP = {
    "id": constants.CODEX_CONNECTOR_APP_ID,
    "slug": constants.CODEX_CONNECTOR_APP_SLUG,
}


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _boundary(
    *,
    run_id: int = RUN_ID,
    occurred_at: datetime = BOUNDARY_AT,
    pull_request: int = PULL_REQUEST,
    head: str = HEAD,
    base: str = BASE,
    base_ref: str = BASE_REF,
) -> provenance.ReviewBoundary:
    return provenance.ReviewBoundary(
        pull_request_number=pull_request,
        head_sha=head,
        base_sha=base,
        base_ref=base_ref,
        occurred_at=occurred_at,
        workflow_run_id=run_id,
    )


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
        "user": {"id": 50_001, "login": "contributor"},
        "draft": draft,
        "state": state,
        "created_at": "2026-08-22T17:00:00Z",
        "updated_at": _timestamp(updated_at),
    }


def _association(
    *, number: int = PULL_REQUEST, head: str = HEAD, state: str = "open"
) -> dict[str, object]:
    return {"number": number, "head": {"sha": head}, "state": state}


def _source_run(
    boundary: provenance.ReviewBoundary | None = None,
    *,
    action: str = "opened",
    base_changed: bool = False,
    path: str = constants.DEFAULT_OBSERVER_WORKFLOW_PATH,
    event: str = "pull_request_target",
    repository: str = REPOSITORY,
    run_head: str | None = None,
    previous_head: str | None = None,
    title_changes: dict[str, object] | None = None,
    created_at: datetime | None = None,
) -> dict[str, object]:
    boundary = boundary or _boundary()
    title: dict[str, object] = {
        "v": 1,
        "pr": boundary.pull_request_number,
        "event": event,
        "head": boundary.head_sha,
        "previous": previous_head or boundary.head_sha,
        "action": action,
        "base": boundary.base_sha,
        "base_ref": boundary.base_ref,
        "base_changed": base_changed,
        "boundary": _timestamp(boundary.occurred_at),
    }
    if title_changes:
        title.update(title_changes)
    return {
        "id": boundary.workflow_run_id,
        "path": path,
        "event": event,
        "repository": {"full_name": repository},
        # A literal live PR #430 run proves that REST workflow-run metadata uses
        # the PR head for pull_request_target. The runner's GITHUB_SHA separately
        # identifies trusted base code; do not conflate those two fields.
        "head_sha": boundary.head_sha if run_head is None else run_head,
        "status": "completed",
        "display_title": json.dumps(title, separators=(",", ":")),
        "created_at": _timestamp(created_at or boundary.occurred_at + timedelta(seconds=10)),
    }


def _review_observer_run(
    *,
    head: str = HEAD,
    actor: dict[str, object] | None = None,
    path: str = constants.DEFAULT_OBSERVER_WORKFLOW_PATH,
) -> dict[str, object]:
    review_actor = copy.deepcopy(actor or CONNECTOR_USER)
    return {
        "id": RUN_ID,
        "path": path,
        "event": "pull_request_review",
        "head_sha": head,
        "status": "completed",
        "conclusion": "success",
        "actor": review_actor,
        "triggering_actor": copy.deepcopy(review_actor),
        "repository": {"full_name": REPOSITORY},
    }


def _workflow_run_event(run: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "repository": {"default_branch": BASE_REF, "full_name": REPOSITORY},
        "workflow_run": copy.deepcopy(run or _review_observer_run()),
    }


def _lifecycle_workflow_run_event(
    run: dict[str, object] | None = None,
) -> dict[str, object]:
    return _workflow_run_event(run or _source_run())


def _status(
    boundary: provenance.ReviewBoundary | None = None,
    *,
    status_id: int = 9_001,
    state: str = "pending",
    created_at: datetime | None = None,
    creator: dict[str, object] | None = None,
    description: object = ...,
    target_url: object = ...,
) -> dict[str, object]:
    boundary = boundary or _boundary()
    if description is ...:
        description = statuses.boundary_description(boundary, state=state)
    if target_url is ...:
        target_url = f"{SERVER_URL}/{REPOSITORY}/actions/runs/{boundary.workflow_run_id}"
    return {
        "id": status_id,
        "state": state,
        "context": constants.STATUS_CONTEXT,
        "description": description,
        "target_url": target_url,
        "created_at": _timestamp(created_at or boundary.occurred_at + timedelta(seconds=20)),
        "creator": copy.deepcopy(creator or ACTIONS_USER),
    }


def _legacy_statuses(count: int) -> list[dict[str, object]]:
    return [
        {
            "id": index,
            "state": "failure",
            "context": constants.STATUS_CONTEXT,
            "description": "legacy terminal state",
            "target_url": None,
            "created_at": _timestamp(BOUNDARY_AT - timedelta(seconds=index)),
            "creator": copy.deepcopy(ACTIONS_USER),
        }
        for index in range(1, count + 1)
    ]


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
        source_runs: dict[int, dict[str, object]] | None = None,
        workflow_history: dict[str, object] | None = None,
        default_branch: str = BASE_REF,
    ) -> None:
        self.pull_requests = {PULL_REQUEST: pull_request or _pull_request()}
        self.associations = associations or [_association()]
        self.comments = comments or []
        self.reviews = reviews or []
        self.reactions = reactions or []
        self.statuses = [_status()] if statuses is None else statuses
        self.statuses_by_head = {HEAD: self.statuses}
        self.source_runs = source_runs or {RUN_ID: _source_run()}
        self.workflow_history = workflow_history
        self.default_branch = default_branch
        self.status_snapshots: list[list[dict[str, object]]] = []
        self.get_errors: dict[str, Exception] = {}
        self.paginate_errors: dict[str, Exception] = {}
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.reads: list[str] = []

    def get(self, path: str) -> object:
        self.reads.append(path)
        if path in self.get_errors:
            raise self.get_errors[path]
        if path == f"/repos/{REPOSITORY}":
            return {"full_name": REPOSITORY, "default_branch": self.default_branch}
        pull_prefix = f"/repos/{REPOSITORY}/pulls/"
        run_prefix = f"/repos/{REPOSITORY}/actions/runs/"
        history_prefix = (
            f"/repos/{REPOSITORY}/actions/workflows/"
            f"{constants.DEFAULT_OBSERVER_WORKFLOW_PATH.rsplit('/', 1)[1]}/runs?"
        )
        commit_prefix = f"/repos/{REPOSITORY}/commits/"
        if path.startswith(pull_prefix) and "?" not in path:
            number = int(path.removeprefix(pull_prefix))
            return copy.deepcopy(self.pull_requests[number])
        if path.startswith(run_prefix):
            run_id = int(path.removeprefix(run_prefix))
            return copy.deepcopy(self.source_runs[run_id])
        if path.startswith(history_prefix):
            if self.workflow_history is not None:
                return copy.deepcopy(self.workflow_history)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            created = query.get("created", [""])[0]
            if not created.startswith(">="):
                raise AssertionError("workflow history omitted its creation bound")
            created_at = datetime.fromisoformat(created.removeprefix(">=").replace("Z", "+00:00"))
            runs = [
                copy.deepcopy(run)
                for run in self.source_runs.values()
                if run.get("path") == constants.DEFAULT_OBSERVER_WORKFLOW_PATH
                and run.get("event") == query.get("event", [None])[0]
                and run.get("head_sha") == query.get("head_sha", [None])[0]
                and run.get("status") == query.get("status", [None])[0]
                and datetime.fromisoformat(str(run["created_at"]).replace("Z", "+00:00"))
                >= created_at
            ]

            def run_key(run: dict[str, object]) -> tuple[str, int]:
                run_id = run["id"]
                assert isinstance(run_id, int) and not isinstance(run_id, bool)
                return str(run["created_at"]), run_id

            runs.sort(key=run_key, reverse=True)
            return {"total_count": len(runs), "workflow_runs": runs[:100]}
        if path.startswith(commit_prefix) and "/" not in path.removeprefix(commit_prefix):
            prefix = path.removeprefix(commit_prefix)
            return {"sha": HEAD if HEAD.startswith(prefix) else "f" * 40}
        status_prefix = f"/repos/{REPOSITORY}/commits/"
        status_suffix = f"/statuses?per_page={constants.MAX_STATUS_PAGE_RECORDS}&page=1"
        if path.startswith(status_prefix) and path.endswith(status_suffix):
            head_sha = path.removeprefix(status_prefix).removesuffix(status_suffix)
            if head_sha == HEAD and self.status_snapshots:
                return copy.deepcopy(self.status_snapshots.pop(0))
            return copy.deepcopy(
                self.statuses_by_head.get(head_sha, [])[-constants.MAX_STATUS_PAGE_RECORDS :]
            )
        if path.startswith(f"/repos/{REPOSITORY}/pulls?state=open"):
            return [
                copy.deepcopy(pull_request)
                for pull_request in self.pull_requests.values()
                if pull_request["state"] == "open"
            ]
        raise AssertionError(f"unexpected GET {path}")

    def paginate(self, path: str) -> list[dict[str, Any]]:
        self.reads.append(path)
        if path in self.paginate_errors:
            raise self.paginate_errors[path]
        if path == f"/repos/{REPOSITORY}/commits/{HEAD}/pulls":
            return copy.deepcopy(self.associations)
        if path == f"/repos/{REPOSITORY}/commits/{HEAD}/statuses":
            if self.status_snapshots:
                return copy.deepcopy(self.status_snapshots.pop(0))
            return copy.deepcopy(self.statuses)
        if path == f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/comments":
            return copy.deepcopy(self.comments)
        if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}/reviews":
            return copy.deepcopy(self.reviews)
        if path == f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/reactions":
            return copy.deepcopy(self.reactions)
        raise AssertionError(f"unexpected pagination {path}")

    def post(self, path: str, payload: dict[str, object]) -> object:
        prefix = f"/repos/{REPOSITORY}/statuses/"
        if not path.startswith(prefix):
            raise AssertionError(f"publisher attempted a non-status write: {path}")
        head_sha = path.removeprefix(prefix)
        self.posts.append((path, copy.deepcopy(payload)))
        record = {
            "id": 10_000 + len(self.posts),
            "state": payload["state"],
            "context": payload["context"],
            "description": payload.get("description"),
            "target_url": payload.get("target_url"),
            "created_at": _timestamp(BOUNDARY_AT + timedelta(hours=2, seconds=len(self.posts))),
            "creator": copy.deepcopy(ACTIONS_USER),
        }
        self.statuses_by_head.setdefault(head_sha, []).append(record)
        return copy.deepcopy(record)


class SuccessRaceApi(FakeApi):
    """Inject a newer lifecycle pending immediately before the first success write."""

    def __init__(
        self,
        *,
        race_boundary: provenance.ReviewBoundary | None = None,
        race_boundaries: tuple[provenance.ReviewBoundary, ...] | None = None,
        reopen: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if (race_boundary is None) == (race_boundaries is None):
            raise ValueError("provide exactly one race boundary source")
        if race_boundaries is None:
            assert race_boundary is not None
            self.race_boundaries = (race_boundary,)
        else:
            self.race_boundaries = race_boundaries
        self.reopen = reopen
        self.race_injected = False

    def post(self, path: str, payload: dict[str, object]) -> object:
        if payload.get("state") == "success" and not self.race_injected:
            self.race_injected = True
            for index, boundary in enumerate(self.race_boundaries):
                self.source_runs[boundary.workflow_run_id] = _source_run(
                    boundary,
                    action="reopened",
                )
                self.statuses.append(
                    _status(
                        boundary,
                        status_id=1_000_000 + index,
                        created_at=boundary.occurred_at + timedelta(seconds=20),
                    )
                )
            if self.reopen:
                self.pull_requests[PULL_REQUEST]["state"] = "open"
                self.associations = [_association(state="open")]
            record = super().post(path, payload)
            return record
        return super().post(path, payload)


def _publish(api: FakeApi) -> str:
    return publisher.reconcile_candidate(
        api,
        repository=REPOSITORY,
        server_url=SERVER_URL,
        candidate=Candidate(
            pull_request_number=PULL_REQUEST,
            head_sha=HEAD,
            base_sha=BASE,
            base_ref=BASE_REF,
            head_repository=HEAD_REPOSITORY,
            head_ref=HEAD_REF,
        ),
        reconciliation_run_id=900_001,
    )


def _publish_candidate(api: FakeApi, candidate: Candidate) -> str:
    return publisher.reconcile_candidate(
        api,
        repository=REPOSITORY,
        server_url=SERVER_URL,
        candidate=candidate,
        reconciliation_run_id=900_001,
    )
