"""Exercise explicit triage and approval admission through the installed CLI."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest
from typer.testing import CliRunner

from yaga.agent_review import remote
from yaga.agent_review.policy import (
    CODEX_DEFAULT_BOT_APP_ID,
    CODEX_DEFAULT_BOT_LOGIN,
    CODEX_DEFAULT_BOT_USER_ID,
)
from yaga.cli import app
from yaga.errors import InputError
from yaga.pull_request_triage import remote as triage_remote
from yaga.pull_request_triage.remote import MARKER

RUNNER = CliRunner()
HEAD = "a" * 40
BASE = "b" * 40
TREE = "c" * 40
OLD = "d" * 40
URL = "https://github.com/example/project/pull/23"
COMMAND = ["pull-request", "triage", "check"]
POLICY_TEXT = """triage-policy-version = 1
host = "github.com"
repository-id = 100
maintainer-user-ids = [10]
duplicate-candidates = 1

[[trusted-bots]]
user-id = 20
login = "dependabot[bot]"
allowed-patterns = ["requirements.txt"]
"""


def acknowledgment(*, text=POLICY_TEXT, head=HEAD, base=BASE, base_ref="main"):
    boundary = {
        "host": "github.com",
        "repository_id": 100,
        "number": 23,
        "head_sha": head,
        "base_sha": base,
        "base_ref": base_ref,
        "policy_digest": hashlib.sha256(text.encode()).hexdigest(),
    }
    digest = hashlib.sha256(
        json.dumps(boundary, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"<!-- yaga-pr-triage-acceptance:v1 {digest} -->"


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / "triage-policy.toml"
    path.write_bytes(POLICY_TEXT.encode())
    return path


def review(*, user_id=10, state="APPROVED", commit=HEAD, number=1, user_type="User"):
    return {
        "id": number,
        "user_id": user_id,
        "user_type": user_type,
        "state": state,
        "commit_id": commit,
        "submitted_at": f"2026-11-11T10:00:{number:02d}Z",
        "body": acknowledgment(),
    }


@pytest.fixture
def snapshot():
    return {
        "version": 1,
        "host": "github.com",
        "repository": "example/project",
        "repository_id": 100,
        "number": 23,
        "state": "open",
        "draft": False,
        "head_sha": HEAD,
        "base_sha": BASE,
        "base_ref": "main",
        "head_tree": TREE,
        "head_repository_id": 100,
        "head_repository_fork": False,
        "author_id": 30,
        "author_login": "contributor",
        "author_type": "User",
        "files": [
            {
                "filename": "requirements.txt",
                "previous_filename": None,
                "sha": OLD,
                "status": "modified",
            }
        ],
        "changed_files": 1,
        "reviews": [],
        "candidates": [],
        "candidate_limit": 1,
        "candidates_examined": 0,
    }


def offline(tmp_path, policy, snapshot):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    result = RUNNER.invoke(
        app, [*COMMAND, "--policy", str(policy), "--file", str(path), "--format", "json"]
    )
    return result, json.loads(result.stdout) if result.stdout else None


@pytest.mark.parametrize("fork", [False, True])
def test_external_same_repo_and_fork_need_current_maintainer_review(
    tmp_path, policy, snapshot, fork
):
    snapshot["head_repository_id"] = 200 if fork else 100
    snapshot["head_repository_fork"] = fork
    result, report = offline(tmp_path, policy, snapshot)
    assert result.exit_code == 1, result.output
    assert report["admission"]["decision"] == "hold"
    assert report["finding_count"] == 0
    snapshot["reviews"] = [review()]
    result, report = offline(tmp_path, policy, snapshot)
    assert result.exit_code == 0, result.output
    assert report["admission"]["reviewer_id"] == 10


@pytest.mark.parametrize(
    "reviews",
    [
        [review(commit=OLD)],
        [review(state="DISMISSED")],
        [review(), review(state="CHANGES_REQUESTED", number=2)],
        [review(user_type="Bot")],
        [review(user_id=999)],
    ],
)
def test_stale_dismissed_superseded_bot_or_unconfigured_review_cannot_admit(
    tmp_path, policy, snapshot, reviews
):
    snapshot["reviews"] = reviews
    result, report = offline(tmp_path, policy, snapshot)
    assert result.exit_code == 1, result.output
    assert report["admission"]["decision"] == "hold"


def test_synchronized_head_invalidates_prior_acceptance(tmp_path, policy, snapshot):
    snapshot["reviews"] = [review()]
    assert offline(tmp_path, policy, snapshot)[0].exit_code == 0
    snapshot["head_sha"] = OLD
    result, report = offline(tmp_path, policy, snapshot)
    assert result.exit_code == 1
    assert report["admission"]["review_id"] is None


@pytest.mark.parametrize(
    "author_id, login, kind, fork, filename, expected",
    [
        (10, "maintainer", "User", False, "source.py", 0),
        (10, "maintainer", "User", True, "source.py", 1),
        (20, "dependabot[bot]", "Bot", False, "requirements.txt", 0),
        (99, "dependabot[bot]", "Bot", False, "requirements.txt", 1),
        (20, "dependabot[bot]", "Bot", True, "requirements.txt", 1),
        (20, "dependabot[bot]", "Bot", False, ".github/workflows/ci.yml", 1),
    ],
)
def test_configured_identity_also_requires_source_and_automation_scope(
    tmp_path, policy, snapshot, author_id, login, kind, fork, filename, expected
):
    snapshot.update(
        author_id=author_id,
        author_login=login,
        author_type=kind,
        head_repository_id=200 if fork else 100,
        head_repository_fork=fork,
    )
    snapshot["files"][0]["filename"] = filename
    result, _ = offline(tmp_path, policy, snapshot)
    assert result.exit_code == expected, result.output


def test_sensitive_old_path_and_duplicate_tree_are_findings_not_malice(tmp_path, policy, snapshot):
    snapshot["files"][0].update(
        filename="docs/notes.md", previous_filename="nested/AGENTS.md", status="renamed"
    )
    snapshot["candidates"] = [{"number": 21, "merge_commit_sha": OLD, "tree_sha": TREE}]
    snapshot["candidates_examined"] = 1
    result, report = offline(tmp_path, policy, snapshot)
    assert result.exit_code == 1
    assert {f["code"] for f in report["findings"]} == {
        "triage.sensitive-path",
        "triage.merged-tree-match",
    }
    assert "malicious" not in report
    assert report["inventory"]["content_analysis"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(changed_files=2),
        lambda s: s.update(head_repository_id=None),
        lambda s: s.update(head_repository_id=200, head_repository_fork=False),
        lambda s: s.update(reviews=[review()] * 100),
        lambda s: s.update(labels=["approved"]),
        lambda s: s["files"][0].update(status="renamed"),
    ],
)
def test_incomplete_or_ambiguous_snapshot_fails_closed(tmp_path, policy, snapshot, mutation):
    mutation(snapshot)
    result, _ = offline(tmp_path, policy, snapshot)
    assert result.exit_code == 2, result.output


class Api:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.comments = []
        self.writes = []
        self.metadata_reads = 0
        self.mutate_at = None
        self.closed = []
        self.tree_reads = 0
        self.mutate_tree_at = None
        self.reads = []
        self.before_get = None
        self.check_conclusion = "success"

    def get(self, executable, reference, endpoint):
        s = self.snapshot
        self.reads.append(endpoint)
        if self.before_get is not None:
            self.before_get(endpoint, s)
        if endpoint == "user":
            return {"id": 55, "login": "publisher", "type": "Bot"}
        if endpoint.endswith("/pulls/23"):
            self.metadata_reads += 1
            if self.mutate_at is not None:
                self.mutate_at(self.metadata_reads, s)
            return {
                "number": 23,
                "html_url": URL,
                "state": s["state"],
                "draft": s["draft"],
                "changed_files": s["changed_files"],
                "user": {
                    "id": s["author_id"],
                    "login": s["author_login"],
                    "type": s["author_type"],
                },
                "head": {
                    "sha": s["head_sha"],
                    "repo": {"id": s["head_repository_id"], "fork": s["head_repository_fork"]},
                },
                "base": {
                    "sha": s["base_sha"],
                    "ref": s["base_ref"],
                    "repo": {"id": s["repository_id"], "full_name": s["repository"]},
                },
                "requested_reviewers": [],
            }
        if "/files?" in endpoint:
            page = int(endpoint.rsplit("page=", 1)[1])
            return copy.deepcopy(s["files"][(page - 1) * 100 : page * 100])
        if "/reviews?" in endpoint:
            return [
                {
                    "id": r["id"],
                    "user": {
                        "id": r["user_id"],
                        "type": r["user_type"],
                        "login": CODEX_DEFAULT_BOT_LOGIN
                        if r["user_id"] == CODEX_DEFAULT_BOT_USER_ID
                        else "maintainer",
                    },
                    "state": r["state"],
                    "commit_id": r["commit_id"],
                    "submitted_at": r["submitted_at"],
                    "body": r["body"],
                }
                for r in s["reviews"]
            ]
        if "/issues/23/comments?" in endpoint:
            return copy.deepcopy(self.comments)
        if "/comments?" in endpoint or "/reactions?" in endpoint:
            return []
        if "/check-runs?" in endpoint:
            return {
                "total_count": 1,
                "check_runs": [
                    {
                        "id": 90,
                        "name": "review-check",
                        "status": "completed",
                        "conclusion": self.check_conclusion,
                        "head_sha": s["head_sha"],
                        "app": {"id": CODEX_DEFAULT_BOT_APP_ID, "slug": "chatgpt-codex-connector"},
                        "started_at": "2026-11-11T10:00:00Z",
                        "completed_at": "2026-11-11T10:00:01Z",
                    }
                ],
            }
        if "/pulls?state=closed" in endpoint:
            return copy.deepcopy(self.closed)
        if "/git/commits/" in endpoint:
            self.tree_reads += 1
            if self.mutate_tree_at is not None:
                self.mutate_tree_at(self.tree_reads, s)
            return {"sha": endpoint.rsplit("/", 1)[-1], "tree": {"sha": s["head_tree"]}}
        raise AssertionError(endpoint)

    def write(self, executable, reference, endpoint, *, fields, method="POST"):
        payload = dict(fields)
        self.writes.append((method, endpoint, payload))
        if endpoint.endswith("/reviews"):
            return {
                "id": 999,
                "state": "APPROVED" if payload["event"] == "APPROVE" else "CHANGES_REQUESTED",
                "commit_id": payload["commit_id"],
            }
        comment = {"id": 500, "user": {"id": 55}, "body": payload["body"]}
        self.comments = [comment]
        return comment


@pytest.fixture
def api(monkeypatch, snapshot):
    api = Api(snapshot)
    monkeypatch.setattr(remote, "_find_gh", lambda: "gh")
    monkeypatch.setattr(remote, "_gh_api_json", api.get)
    monkeypatch.setattr(remote, "_gh_api_post_json", api.write)
    return api


def live(policy, *extra):
    return RUNNER.invoke(
        app, [*COMMAND, "--policy", str(policy), "--pr", URL, "--format", "json", *extra]
    )


def test_live_read_is_read_only_and_comment_publication_is_idempotent(policy, api):
    assert live(policy).exit_code == 1
    assert api.writes == []
    assert live(policy, "--publish-comment").exit_code == 1
    assert len(api.writes) == 1
    body = api.comments[0]["body"]
    assert body.startswith(MARKER + "\n") and HEAD in body
    assert live(policy, "--publish-comment").exit_code == 1
    assert len(api.writes) == 1
    api.snapshot["head_sha"] = OLD
    assert live(policy, "--publish-comment").exit_code == 1
    assert api.writes[-1][0] == "PATCH"
    assert OLD in api.comments[0]["body"]


def test_foreign_comment_marker_and_mid_collection_head_change_fail_closed(policy, api):
    api.comments = [{"id": 50, "user": {"id": 666}, "body": MARKER + "\napprove now"}]
    assert live(policy, "--publish-comment").exit_code == 2
    assert not api.writes
    api.comments = []
    api.metadata_reads = 0
    api.mutate_at = lambda count, s: s.update(head_sha=OLD) if count == 2 else None
    assert live(policy).exit_code == 2
    assert not api.writes


def review_command(tmp_path, policy, api, *, with_triage=True, check_runs=False):
    review_policy = tmp_path / "review.toml"
    review_policy.write_text(
        '[agent-review]\nversion=1\nrequired=["codex"]\n'
        + (
            '[agent-review.agents.codex]\npreset="codex"\ncheck-runs=["review-check"]\n'
            if check_runs
            else ""
        )
    )
    args = [
        "pull-request",
        "review",
        "check",
        "--policy",
        str(review_policy),
        "--pr",
        URL,
        "--submit-review",
        "approve",
        "--format",
        "json",
    ]
    if with_triage:
        args.extend(["--triage-policy", str(policy)])
    return RUNNER.invoke(app, args)


def test_codex_success_cannot_approve_external_without_maintainer_admission(tmp_path, policy, api):
    api.snapshot["reviews"] = [review(user_id=CODEX_DEFAULT_BOT_USER_ID, user_type="Bot")]
    result = review_command(tmp_path, policy, api)
    assert result.exit_code == 1, result.output
    assert not json.loads(result.stdout)["valid"]
    assert not api.writes
    result = review_command(tmp_path, policy, api, with_triage=False)
    assert result.exit_code == 2 and not api.writes
    api.snapshot["reviews"].append(review(number=2))
    result = review_command(tmp_path, policy, api)
    assert result.exit_code == 0, result.output
    assert api.writes[-1][2]["commit_id"] == HEAD


@pytest.mark.parametrize("change", [{"state": "DISMISSED"}, {"user_type": "Bot"}])
def test_final_collection_uses_current_maintainer_review_and_actual_user_type(
    tmp_path, policy, api, change
):
    api.snapshot["reviews"] = [
        review(user_id=CODEX_DEFAULT_BOT_USER_ID, user_type="Bot"),
        review(number=2),
    ]

    def mutate(endpoint, snapshot):
        if api.tree_reads and "/comments?" in endpoint:
            snapshot["reviews"][1].update(change)

    api.before_get = mutate
    result = review_command(tmp_path, policy, api)
    assert result.exit_code == 1, result.output
    assert not api.writes


@pytest.mark.parametrize("change", ["head_sha", "base_sha", "base_ref", "policy"])
def test_acknowledgment_binds_head_base_branch_and_trusted_policy(
    tmp_path, policy, snapshot, change
):
    snapshot["reviews"] = [review()]
    assert offline(tmp_path, policy, snapshot)[0].exit_code == 0
    text = POLICY_TEXT
    if change == "policy":
        text += "# changed trusted policy bytes\n"
        policy.write_bytes(text.encode())
    else:
        snapshot[change] = "release" if change == "base_ref" else OLD
    result, report = offline(tmp_path, policy, snapshot)
    assert result.exit_code == 1
    assert report["admission"]["decision"] == "hold"
    snapshot["reviews"][0].update(
        commit_id=snapshot["head_sha"],
        body=acknowledgment(
            text=text,
            head=snapshot["head_sha"],
            base=snapshot["base_sha"],
            base_ref=snapshot["base_ref"],
        ),
    )
    assert offline(tmp_path, policy, snapshot)[0].exit_code == 0


@pytest.mark.parametrize("body", ["", "Approved", acknowledgment() * 2, " " + acknowledgment()])
def test_ordinary_or_ambiguous_review_needs_explicit_boundary_acknowledgment(
    tmp_path, policy, snapshot, body
):
    snapshot["reviews"] = [dict(review(), body=body)]
    result, report = offline(tmp_path, policy, snapshot)
    assert result.exit_code == 1
    assert report["admission"]["reason"] == "maintainer-acknowledgment-required"
    assert report["admission"]["acknowledgment"] == acknowledgment()


def test_fractional_review_timestamp_orders_later_change_request(tmp_path, policy, snapshot):
    earlier = dict(review(), submitted_at="2026-11-11T10:00:01Z")
    later = dict(
        review(number=2, state="CHANGES_REQUESTED"), submitted_at="2026-11-11T10:00:01.500Z"
    )
    snapshot["reviews"] = [later, earlier]
    result, report = offline(tmp_path, policy, snapshot)
    assert result.exit_code == 1
    assert report["admission"]["reason"] == "maintainer-changes-requested"


@pytest.mark.parametrize(
    "replacement",
    [
        "triage-policy-version = true",
        "triage-policy-version = 2",
        "triage-policy-version = 1\nunrecognized = true",
    ],
)
def test_invalid_policy_errors_are_bounded_triage_reports(tmp_path, policy, snapshot, replacement):
    policy.write_bytes(POLICY_TEXT.replace("triage-policy-version = 1", replacement).encode())
    result, _ = offline(tmp_path, policy, snapshot)
    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["kind"] == "pull_request_triage" and error["status"] == "error"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(state=[]),
        lambda s: s.update(author_type={}),
        lambda s: s["files"][0].update(status=[]),
        lambda s: s["files"][0].update(filename="bad\ud800.txt"),
        lambda s: s.update(reviews=[dict(review(), body="bad\ud800")]),
        lambda s: s.update(reviews=[dict(review(), body=None)]),
        lambda s: s.update(reviews=[dict(review(), body="x" * 65537)]),
    ],
)
def test_malformed_metadata_cannot_escape_as_uncaught_exception(
    tmp_path, policy, snapshot, mutation
):
    mutation(snapshot)
    result, _ = offline(tmp_path, policy, snapshot)
    assert result.exit_code == 2
    assert json.loads(result.stderr)["status"] == "error"


def test_complete_paginated_files_and_exact_merged_tree_finding(policy, api):
    api.snapshot["files"] = [
        dict(api.snapshot["files"][0], filename=f"src/file{index}.py") for index in range(101)
    ]
    api.snapshot["changed_files"] = 101
    api.closed = [
        {
            "number": 21,
            "state": "closed",
            "html_url": URL.replace("23", "21"),
            "merged_at": "2026-11-11T10:00:00Z",
            "merge_commit_sha": OLD,
        }
    ]
    result = live(policy)
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["inventory"]["changed_files"] == 101
    assert report["findings"] == [
        {"code": "triage.merged-tree-match", "pull_request_number": 21, "merge_commit_sha": OLD}
    ]
    api.snapshot["changed_files"] = 102
    assert live(policy).exit_code == 2
    assert not api.writes


@pytest.mark.parametrize(
    "mutation",
    [
        lambda a: a.snapshot.update(changed_files=501),
        lambda a: a.snapshot.update(reviews=[review()] * 100),
        lambda a: setattr(a, "closed", [{"number": 1, "state": "closed", "merged_at": None}]),
        lambda a: setattr(a, "comments", [{"id": i, "body": "plain"} for i in range(100)]),
    ],
)
def test_incomplete_live_inventory_never_writes(policy, api, mutation):
    mutation(api)
    assert live(policy, "--publish-comment").exit_code == 2
    assert not api.writes


def test_comment_samples_paths_as_inert_text_and_carries_reviewable_boundary(policy, api):
    hostile = ".github/<img src=x onerror=alert(1)>`[approve](evil)\u202e.yml"
    api.snapshot["files"] = [dict(api.snapshot["files"][0], filename=hostile)] + [
        dict(api.snapshot["files"][0], filename=f".github/workflows/{i}.yml") for i in range(11)
    ]
    api.snapshot["changed_files"] = 12
    result = live(policy, "--publish-comment")
    assert result.exit_code == 1
    body = api.comments[0]["body"]
    assert "<img" not in body and "\u202e" not in body
    assert "&lt;img" in body and "&#96;" in body and "\\u202e" in body
    assert body.count("- triage.sensitive-path:") == 10
    assert "Findings: 12" in body and acknowledgment() in body
    assert BASE in body and hashlib.sha256(POLICY_TEXT.encode()).hexdigest() in body


def test_final_comment_revalidation_reports_revoked_admission(policy, api):
    api.snapshot["reviews"] = [review()]
    api.mutate_at = lambda count, s: (
        s["reviews"][0].update(state="DISMISSED") if count == 3 else None
    )
    result = live(policy, "--publish-comment")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["admission"]["decision"] == "hold"
    assert "**hold**" in api.comments[0]["body"]


def test_configured_comment_publisher_and_duplicate_marker_fail_closed(policy, api, monkeypatch):
    policy.write_bytes(
        POLICY_TEXT.replace(
            "duplicate-candidates = 1", "duplicate-candidates = 1\ncomment-author-id = 55"
        ).encode()
    )
    original = api.get

    def without_user(executable, reference, endpoint):
        assert endpoint != "user"
        return original(executable, reference, endpoint)

    monkeypatch.setattr(remote, "_gh_api_json", without_user)
    assert live(policy, "--publish-comment").exit_code == 1
    api.comments.append(dict(api.comments[0], id=501))
    previous = len(api.writes)
    assert live(policy, "--publish-comment").exit_code == 2
    assert len(api.writes) == previous


def test_dismissal_during_final_duplicate_scan_prevents_approval(tmp_path, policy, api):
    api.snapshot["reviews"] = [
        review(user_id=CODEX_DEFAULT_BOT_USER_ID, user_type="Bot"),
        review(number=2),
    ]
    api.closed = [
        {
            "number": 21,
            "state": "closed",
            "html_url": URL.replace("23", "21"),
            "merged_at": "2026-11-11T10:00:00Z",
            "merge_commit_sha": OLD,
        }
    ]
    # One inventory scan sees a dismissal during a candidate tree request,
    # before code-review and admission share their final mutable collection.
    api.mutate_tree_at = lambda count, s: (
        s["reviews"][1].update(state="DISMISSED") if count == 1 else None
    )
    result = review_command(tmp_path, policy, api)
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["triage"]["admission"]["decision"] == "hold"
    assert not api.writes


def test_api_request_limit_rejects_next_request_without_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(remote, "_find_gh", lambda: "gh")
    monkeypatch.setattr(remote, "_gh_api_json", lambda *args: calls.append(args) or {})
    monkeypatch.setattr(triage_remote.time, "monotonic", lambda: 0)
    client = triage_remote.TriageClient(remote.parse_pull_request_reference(URL))
    for _ in range(64):
        client.get("user")
    with pytest.raises(InputError, match="budget exhausted"):
        client.get("user")
    assert len(calls) == 64


@pytest.mark.parametrize("state", ["DISMISSED", "CHANGES_REQUESTED"])
def test_agent_verdict_revoked_during_tree_work_prevents_approval(tmp_path, policy, api, state):
    api.snapshot["reviews"] = [
        review(user_id=CODEX_DEFAULT_BOT_USER_ID, user_type="Bot"),
        review(number=2),
    ]
    api.mutate_tree_at = lambda count, s: s["reviews"][0].update(state=state)
    result = review_command(tmp_path, policy, api)
    assert result.exit_code == 2, result.output
    assert not api.writes


def test_final_publication_reads_shared_reviews_after_all_immutable_work(tmp_path, policy, api):
    api.snapshot["reviews"] = [
        review(user_id=CODEX_DEFAULT_BOT_USER_ID, user_type="Bot"),
        review(number=2),
    ]
    result = review_command(tmp_path, policy, api)
    assert result.exit_code == 0, result.output
    tree_index = max(i for i, endpoint in enumerate(api.reads) if "/git/commits/" in endpoint)
    comment_index = max(i for i, endpoint in enumerate(api.reads) if "/comments?" in endpoint)
    reviews_index = max(i for i, endpoint in enumerate(api.reads) if "/reviews?" in endpoint)
    metadata_index = max(
        i for i, endpoint in enumerate(api.reads) if endpoint.endswith("/pulls/23")
    )
    assert tree_index < comment_index < reviews_index < metadata_index
    assert api.tree_reads == 1
    assert sum("/reviews?" in endpoint for endpoint in api.reads) == 2
    assert api.writes[-1][2]["commit_id"] == HEAD


def test_maximum_inventory_keeps_joint_publication_within_request_budget(tmp_path, policy, api):
    policy.write_text(POLICY_TEXT.replace("duplicate-candidates = 1", "duplicate-candidates = 20"))
    api.snapshot.update(author_id=10, author_login="maintainer", changed_files=500)
    api.snapshot["files"] = [
        dict(api.snapshot["files"][0], filename=f"source/{number}.py") for number in range(500)
    ]
    api.snapshot["reviews"] = [review(user_id=CODEX_DEFAULT_BOT_USER_ID, user_type="Bot")]
    api.closed = [
        {
            "number": number,
            "state": "closed",
            "html_url": f"https://github.com/example/project/pull/{number}",
            "merged_at": "2026-11-11T10:00:00Z",
            "merge_commit_sha": f"{number:040x}",
        }
        for number in range(1, 21)
    ]
    result = review_command(tmp_path, policy, api, check_runs=True)
    assert result.exit_code == 0, result.output
    assert api.tree_reads == 21
    assert sum("/files?" in endpoint for endpoint in api.reads) == 5
    assert len(api.reads) + len(api.writes) < 64
    assert api.writes[-1][2]["commit_id"] == HEAD


@pytest.mark.parametrize("field", ["head_sha", "base_sha", "base_ref"])
def test_boundary_change_during_final_mutable_collection_prevents_write(
    tmp_path, policy, api, field
):
    api.snapshot["reviews"] = [
        review(user_id=CODEX_DEFAULT_BOT_USER_ID, user_type="Bot"),
        review(number=2),
    ]

    def mutate(endpoint, snapshot):
        if api.tree_reads and "/comments?" in endpoint:
            snapshot[field] = "release" if field == "base_ref" else OLD

    api.before_get = mutate
    result = review_command(tmp_path, policy, api)
    assert result.exit_code == 2, result.output
    assert not api.writes


def test_agent_check_run_failure_during_tree_work_prevents_approval(tmp_path, policy, api):
    api.snapshot["reviews"] = [
        review(user_id=CODEX_DEFAULT_BOT_USER_ID, user_type="Bot"),
        review(number=2),
    ]
    api.mutate_tree_at = lambda count, s: setattr(api, "check_conclusion", "failure")
    result = review_command(tmp_path, policy, api, check_runs=True)
    assert result.exit_code == 2, result.output
    assert json.loads(result.stderr)["error"]["message"] == (
        "PR review boundary or outcome changed before submission"
    )
    assert not api.writes


def test_agent_summary_failure_during_tree_work_prevents_approval(tmp_path, policy, api):
    api.snapshot["reviews"] = [
        review(user_id=CODEX_DEFAULT_BOT_USER_ID, user_type="Bot"),
        review(number=2),
    ]

    def mutate(count, snapshot):
        state = {
            "code_review": {
                "commit_sha": HEAD,
                "status": "completed",
                "has_any_findings": True,
                "has_unresolved_findings": True,
            }
        }
        api.comments = [
            {
                "id": 90,
                "user": {"id": CODEX_DEFAULT_BOT_USER_ID, "login": CODEX_DEFAULT_BOT_LOGIN},
                "performed_via_github_app": {"id": CODEX_DEFAULT_BOT_APP_ID},
                "created_at": "2026-11-11T10:00:03Z",
                "updated_at": "2026-11-11T10:00:03Z",
                "body": "<!-- codex-review-state:v1 " + json.dumps(state) + " -->",
            }
        ]

    api.mutate_tree_at = mutate
    result = review_command(tmp_path, policy, api)
    assert result.exit_code == 2, result.output
    assert json.loads(result.stderr)["error"]["message"] == (
        "PR review boundary or outcome changed before submission"
    )
    assert not api.writes


def test_api_deadline_reserves_each_process_timeout_before_launch(monkeypatch):
    now = [0.0]
    calls = []
    monkeypatch.setattr(remote, "_find_gh", lambda: "gh")
    monkeypatch.setattr(remote, "_gh_api_json", lambda *args: calls.append(args) or {})
    monkeypatch.setattr(triage_remote.time, "monotonic", lambda: now[0])
    client = triage_remote.TriageClient(remote.parse_pull_request_reference(URL))
    now[0] = 270
    client.get("user")
    now[0] = 270.001
    with pytest.raises(InputError, match="budget exhausted"):
        client.get("user")
    assert len(calls) == 1


def test_duplicate_scan_excludes_target_pr_and_preserves_candidate_window(
    tmp_path, policy, snapshot, api
):
    self_commit = "e" * 40
    api.snapshot["state"] = "closed"
    api.closed = [
        {
            "number": 23,
            "state": "closed",
            "html_url": URL,
            "merged_at": "2026-11-11T10:02:00Z",
            "merge_commit_sha": self_commit,
        },
        {
            "number": 21,
            "state": "closed",
            "html_url": URL.replace("23", "21"),
            "merged_at": "2026-11-11T10:01:00Z",
            "merge_commit_sha": OLD,
        },
    ]
    result = live(policy)
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["inventory"]["candidates_examined"] == 1
    assert report["findings"] == [
        {"code": "triage.merged-tree-match", "pull_request_number": 21, "merge_commit_sha": OLD}
    ]
    assert not any(endpoint.endswith(f"/git/commits/{self_commit}") for endpoint in api.reads)

    # When only the target PR itself is closed, zero external candidates are examined.
    api.reads.clear()
    api.closed = api.closed[:1]
    result = live(policy)
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["inventory"]["candidates_examined"] == 0
    assert report["findings"] == []

    # Offline snapshots containing a self-referential candidate fail closed.
    snapshot["candidates"] = [{"number": 23, "merge_commit_sha": OLD, "tree_sha": TREE}]
    snapshot["candidates_examined"] = 1
    offline_result, _ = offline(tmp_path, policy, snapshot)
    assert offline_result.exit_code == 2


def test_live_null_bodies_uppercase_shas_and_gcd_review_commits_behave_safely(
    tmp_path, policy, snapshot, api
):
    # Live GitHub REST reviews and issue comments can have body=None, uppercase commit_id,
    # and historical reviews whose force-pushed commit was garbage-collected (commit_id=None).
    api.comments = [{"id": 77, "user": {"id": 999}, "body": None}]
    api.snapshot["reviews"] = [
        {
            "id": 1,
            "user_id": 10,
            "user_type": "User",
            "state": "APPROVED",
            "commit_id": HEAD.upper(),
            "submitted_at": "2026-11-11T10:00:01Z",
            "body": acknowledgment(),
        },
        {
            "id": 2,
            "user_id": 999,
            "user_type": "User",
            "state": "COMMENTED",
            "commit_id": None,
            "submitted_at": "2026-11-11T10:00:02Z",
            "body": None,
        },
    ]
    result = live(policy, "--publish-comment")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["admission"]["decision"] == "allow"

    # A configured maintainer's later CHANGES_REQUESTED with a GC'd commit_id=None still holds.
    api.snapshot["reviews"].append(
        {
            "id": 3,
            "user_id": 10,
            "user_type": "User",
            "state": "CHANGES_REQUESTED",
            "commit_id": None,
            "submitted_at": "2026-11-11T10:00:03Z",
            "body": None,
        }
    )
    held = live(policy)
    assert held.exit_code == 1, held.output
    assert json.loads(held.stdout)["admission"]["reason"] == "maintainer-changes-requested"

    # An APPROVED review with commit_id=None cannot admit even with an acknowledgment body.
    snapshot["reviews"] = [dict(review(), commit_id=None)]
    offline_held, offline_report = offline(tmp_path, policy, snapshot)
    assert offline_held.exit_code == 1
    assert offline_report["admission"]["decision"] == "hold"


def test_failed_review_with_admission_hold_preserves_failed_status_on_submit_both(
    tmp_path, policy, api
):
    review_policy = tmp_path / "review.toml"
    review_policy.write_text('[agent-review]\nversion=1\nrequired=["codex"]\n')
    api.snapshot["reviews"] = [
        review(
            user_id=CODEX_DEFAULT_BOT_USER_ID,
            user_type="Bot",
            state="CHANGES_REQUESTED",
        )
    ]
    result = RUNNER.invoke(
        app,
        [
            "pull-request",
            "review",
            "check",
            "--policy",
            str(review_policy),
            "--pr",
            URL,
            "--submit-review",
            "both",
            "--triage-policy",
            str(policy),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["valid"] is False
    assert doc["status"] == "failed"
    assert doc["blocking_failures"] == ["codex"]
    assert doc["triage"]["admission"]["decision"] == "hold"
    assert doc["submitted_review"]["state"] == "CHANGES_REQUESTED"
    assert api.writes[-1][2]["event"] == "REQUEST_CHANGES"
