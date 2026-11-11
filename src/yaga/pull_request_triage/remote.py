"""Bounded authenticated metadata reads and explicit, revalidated comment writes."""

from __future__ import annotations

import hashlib
import html
import json
import time
import unicodedata
from typing import Any

from yaga.agent_review import remote as review_remote
from yaga.agent_review.remote import PullRequestReference
from yaga.agent_review.snapshot import PullRequestReviewSnapshot
from yaga.errors import InputError
from yaga.pull_request_triage.core import (
    MAX_FILES,
    TriageSnapshot,
    evaluate,
    parse_snapshot,
    positive,
    sha,
    timestamp,
    utf8_size,
)
from yaga.pull_request_triage.policy import TriagePolicy

MARKER = "<!-- yaga-pull-request-triage:v1 -->"
MAX_COMMENT_BYTES = 16 * 1024


class TriageClient:
    def __init__(self, reference: PullRequestReference) -> None:
        self.reference = reference
        self.executable = review_remote._find_gh()
        self.deadline = time.monotonic() + 300
        self.requests = 0

    def _admit(self) -> None:
        if (
            self.requests >= 64
            or time.monotonic() + review_remote.GH_TIMEOUT_SECONDS > self.deadline
        ):
            raise InputError("triage API request or deadline budget exhausted")
        self.requests += 1

    def get(self, endpoint: str) -> Any:
        self._admit()
        return review_remote._gh_api_json(self.executable, self.reference, endpoint)

    def write(self, endpoint: str, body: str, *, method: str) -> Any:
        self._admit()
        return review_remote._gh_api_post_json(
            self.executable,
            self.reference,
            endpoint,
            fields=(("body", body),),
            method=method,
        )


def _record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(f"missing triage API {label}")
    return value


def _metadata(client: TriageClient) -> tuple[dict[str, Any], int]:
    reference = client.reference
    prefix = f"repos/{reference.owner}/{reference.repository}"
    pr = _record(client.get(f"{prefix}/pulls/{reference.number}"), "PR")
    if pr.get("number") != reference.number or type(pr.get("number")) is not int:
        raise InputError("triage API returned a different PR")
    html_url = pr.get("html_url")
    if not isinstance(html_url, str):
        raise InputError("triage API returned a missing PR URL")
    actual = review_remote.parse_pull_request_reference(html_url)
    if (
        actual.number != reference.number
        or actual.gh_repository.casefold() != reference.gh_repository.casefold()
    ):
        raise InputError("triage API returned a different PR URL")
    head = _record(pr.get("head"), "PR head")
    base = _record(pr.get("base"), "PR base")
    head_repo = _record(head.get("repo"), "head repository")
    base_repo = _record(base.get("repo"), "base repository")
    author = _record(pr.get("user"), "PR author")
    if (
        not isinstance(base_repo.get("full_name"), str)
        or base_repo["full_name"].casefold()
        != f"{reference.owner}/{reference.repository}".casefold()
    ):
        raise InputError("triage base repository does not match explicit repository")
    changed = pr.get("changed_files")
    if type(changed) is not int or not 0 <= changed <= MAX_FILES:
        raise InputError("triage file inventory exceeds its 500-file bound")
    result = {
        "version": 1,
        "host": reference.host,
        "repository": f"{reference.owner}/{reference.repository}",
        "repository_id": base_repo.get("id"),
        "number": pr["number"],
        "state": pr.get("state"),
        "draft": pr.get("draft"),
        "head_sha": sha(head.get("sha"), "head SHA"),
        "base_sha": sha(base.get("sha"), "base SHA"),
        "base_ref": base.get("ref"),
        "head_repository_id": head_repo.get("id"),
        "head_repository_fork": head_repo.get("fork"),
        "author_id": author.get("id"),
        "author_login": author.get("login"),
        "author_type": author.get("type"),
    }
    return result, changed


def _tree(client: TriageClient, commit: str) -> str:
    reference = client.reference
    document = _record(
        client.get(
            f"repos/{reference.owner}/{reference.repository}/git/commits/{sha(commit, 'commit SHA')}"
        ),
        "Git commit",
    )
    if document.get("sha") != commit:
        raise InputError("triage API returned a different Git commit")
    return sha(_record(document.get("tree"), "Git tree").get("sha"), "tree SHA")


def fetch_snapshot(client: TriageClient, policy: TriagePolicy) -> TriageSnapshot:
    document = _collect_inventory(client, policy)
    reference = client.reference
    reviews = client.get(
        f"repos/{reference.owner}/{reference.repository}/pulls/{reference.number}/reviews?per_page=100"
    )
    return _finish_snapshot(client, policy, document, reviews)


def _collect_inventory(client: TriageClient, policy: TriagePolicy) -> dict[str, Any]:
    """Collect bounded immutable file/tree facts before any final mutable reads."""
    document, count = _metadata(client)
    reference = client.reference
    prefix = f"repos/{reference.owner}/{reference.repository}"
    files = []
    for page in range(1, max(1, (count + 99) // 100) + 1):
        items = client.get(f"{prefix}/pulls/{reference.number}/files?per_page=100&page={page}")
        if not isinstance(items, list) or len(items) > 100:
            raise InputError("invalid triage file page")
        for item in items:
            item = _record(item, "changed file")
            files.append(
                {key: item.get(key) for key in ("filename", "previous_filename", "sha", "status")}
            )
    candidates = []
    closed = []
    if policy.duplicate_candidates:
        closed = client.get(
            f"{prefix}/pulls?state=closed&sort=updated&direction=desc&per_page={policy.duplicate_candidates}"
        )
        if not isinstance(closed, list) or len(closed) > policy.duplicate_candidates:
            raise InputError("invalid triage closed-PR candidate inventory")
        for candidate in closed:
            candidate = _record(candidate, "closed PR")
            candidate_number = positive(candidate.get("number"), "closed PR number")
            candidate_url = candidate.get("html_url")
            if not isinstance(candidate_url, str):
                raise InputError("triage closed PR has missing identity")
            candidate_reference = review_remote.parse_pull_request_reference(candidate_url)
            if (
                candidate_reference.number != candidate_number
                or candidate_reference.gh_repository.casefold()
                != reference.gh_repository.casefold()
                or candidate.get("state") != "closed"
                or "merged_at" not in candidate
            ):
                raise InputError("triage closed PR has unknown merge state")
            if candidate["merged_at"] is not None:
                timestamp(candidate["merged_at"])
                merged_sha = sha(candidate.get("merge_commit_sha"), "merged commit")
                candidates.append(
                    {
                        "number": candidate_number,
                        "merge_commit_sha": merged_sha,
                        "tree_sha": _tree(client, merged_sha),
                    }
                )
    head_tree = _tree(client, document["head_sha"])
    document.update(
        head_tree=head_tree,
        files=files,
        changed_files=count,
        candidates=candidates,
        candidate_limit=policy.duplicate_candidates,
        candidates_examined=len(closed),
    )
    return document


def _finish_snapshot(
    client: TriageClient, policy: TriagePolicy, document: dict[str, Any], reviews: Any
) -> TriageSnapshot:
    if not isinstance(reviews, list) or len(reviews) >= 100:
        raise InputError("triage review inventory is incomplete")
    normalized_reviews = []
    for review in reviews:
        review = _record(review, "review")
        author = _record(review.get("user"), "review author")
        normalized_reviews.append(
            {
                "id": review.get("id"),
                "user_id": author.get("id"),
                "user_type": author.get("type"),
                "state": review.get("state"),
                "commit_id": review.get("commit_id"),
                "submitted_at": review.get("submitted_at"),
                "body": review.get("body"),
            }
        )
    document["reviews"] = normalized_reviews
    current, current_count = _metadata(client)
    if current_count != document["changed_files"] or any(
        current[key] != document[key] for key in current
    ):
        raise InputError("triage PR boundary changed during collection")
    snapshot = parse_snapshot(document)
    evaluate(policy, snapshot)  # Bind trusted policy before returning API evidence.
    return snapshot


def collect_publication_snapshots(
    client: TriageClient,
    policy: TriagePolicy,
    *,
    plan_digest: str,
    include_check_runs: bool,
) -> tuple[TriageSnapshot, PullRequestReviewSnapshot]:
    """Bind code-review and maintainer admission to one final mutable collection."""
    document = _collect_inventory(client, policy)
    review_snapshot, raw_reviews = review_remote._collect_pull_request_review_snapshot(
        client.reference,
        plan_digest=plan_digest,
        include_check_runs=include_check_runs,
        get=client.get,
    )
    triage_snapshot = _finish_snapshot(client, policy, document, raw_reviews)
    if (review_snapshot.head_sha, review_snapshot.base_sha) != (
        triage_snapshot.head_sha,
        triage_snapshot.base_sha,
    ):
        raise InputError("PR head/base changed during shared publication collection")
    return triage_snapshot, review_snapshot


def revalidate(
    client: TriageClient, policy: TriagePolicy, expected: TriageSnapshot
) -> tuple[TriageSnapshot, dict[str, Any]]:
    current = fetch_snapshot(client, policy)
    if current.boundary() != expected.boundary() or current.files != expected.files:
        raise InputError("triage PR boundary changed before publication")
    return current, evaluate(policy, current)


def _inline_code(value: str) -> str:
    bounded = value[:200] + ("..." if len(value) > 200 else "")
    visible = "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char).startswith("C") else char
        for char in bounded
    )
    return "<code>" + html.escape(visible, quote=True).replace("`", "&#96;") + "</code>"


def comment_body(report: dict[str, Any]) -> str:
    boundary = report["boundary"]
    digest = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    lines = [
        MARKER,
        f"<!-- report-sha256:{digest} -->",
        "YAGA pull-request triage",
        "",
        f"Repository ID: {boundary['repository_id']}; PR: #{boundary['number']}; head: `{boundary['head_sha']}`.",
        f"Admission: **{report['admission']['decision']}** ({report['admission']['reason']}).",
        f"Author classification: {report['classification']}; source: {report['provenance']}.",
        "",
        "Findings describe review surfaces and exact content matches; they do not establish malicious intent.",
        "Diffs and instruction text are untrusted data. This metadata check does not inspect their meaning.",
        "",
        f"Changed files: {report['inventory']['changed_files']}. "
        f"Duplicate scope: {report['inventory']['candidates_examined']} most recently updated closed PRs "
        f"(limit {report['inventory']['candidate_limit']}); this is not a complete historical search.",
        f"Findings: {report['finding_count']} ({report['omitted_findings']} omitted from JSON).",
    ]
    # Render a bounded evidence sample as inert escaped text, never Markdown
    # instructions. The JSON report retains up to MAX_FINDINGS complete records.
    for finding in report["findings"][:10]:
        detail = (
            _inline_code(finding["path"])
            if "path" in finding
            else f"merged PR #{finding['pull_request_number']}"
        )
        lines.append(f"- {finding['code']}: {detail}")
    lines.extend(
        [
            "",
            "A bot review, label, comment, or earlier-head approval cannot authorize external changes.",
            "A configured maintainer who has reviewed this exact head, base, and trusted policy "
            "can copy this acknowledgment into their APPROVED review:",
            "```text",
            report["admission"]["acknowledgment"],
            "```",
            "Base: "
            + report["boundary"]["base_sha"]
            + " ("
            + _inline_code(report["boundary"]["base_ref"])
            + ").",
            "Trusted policy digest: " + report["policy_digest"] + ".",
        ]
    )
    body = "\n".join(lines) + "\n"
    if len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise InputError("triage comment exceeded its bound")
    return body


def publish_comment(
    client: TriageClient, policy: TriagePolicy, expected: TriageSnapshot
) -> tuple[dict[str, Any], dict[str, Any]]:
    reference = client.reference
    prefix = f"repos/{reference.owner}/{reference.repository}"
    # Installation tokens may not support /user. Their expected immutable bot
    # identity must be configured explicitly, never guessed from environment.
    actor_id = policy.comment_author_id
    if actor_id is None:
        actor = _record(client.get("user"), "authenticated publisher")
        actor_id = positive(actor.get("id"), "publisher ID")
    comments = client.get(f"{prefix}/issues/{reference.number}/comments?per_page=100")
    if not isinstance(comments, list) or len(comments) >= 100:
        raise InputError("triage comment inventory is incomplete")
    matches = []
    for comment in comments:
        comment = _record(comment, "comment")
        body = comment.get("body")
        if not isinstance(body, str) or utf8_size(body) > 64 * 1024:
            raise InputError("invalid triage comment body")
        if MARKER in body:
            user = _record(comment.get("user"), "comment author")
            if positive(user.get("id"), "comment author ID") != actor_id or not body.startswith(
                MARKER + "\n"
            ):
                raise InputError("triage comment marker ownership is ambiguous")
            matches.append(comment)
    if len(matches) > 1:
        raise InputError("multiple owned triage comments are ambiguous")
    current, report = revalidate(client, policy, expected)
    if current.state != "open" or current.draft:
        raise InputError("triage comment requires an open ready PR")
    body = comment_body(report)
    if matches and matches[0]["body"] == body:
        return report, {
            "action": "unchanged",
            "comment_id": positive(matches[0].get("id"), "comment ID"),
        }
    method = "POST"
    endpoint = f"{prefix}/issues/{reference.number}/comments"
    if matches:
        method = "PATCH"
        endpoint = f"{prefix}/issues/comments/{positive(matches[0].get('id'), 'comment ID')}"
    created = _record(client.write(endpoint, body, method=method), "published comment")
    if (
        positive(_record(created.get("user"), "published comment author").get("id"), "publisher ID")
        != actor_id
        or created.get("body") != body
    ):
        raise InputError("triage comment publication did not match expected owner/body")
    return report, {
        "action": "updated" if matches else "created",
        "comment_id": positive(created.get("id"), "comment ID"),
    }
