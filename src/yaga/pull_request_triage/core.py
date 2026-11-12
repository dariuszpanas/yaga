"""Pure bounded triage over explicit metadata, never PR instructions or code."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from yaga.errors import InputError
from yaga.path_patterns import _compile_components, match_compiled, validate_pattern
from yaga.pull_request_triage.policy import TriagePolicy

MAX_FILES = 500
MAX_REVIEWS = 99
MAX_FINDINGS = 256
MAX_SNAPSHOT_BYTES = 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}")
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\[\]-]{0,99}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
ACCEPTANCE_PREFIX = "<!-- yaga-pr-triage-acceptance:v1 "


def positive(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= (1 << 53) - 1:
        raise InputError(f"invalid triage {label}")
    return value


def sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise InputError(f"invalid triage {label}")
    return value


def obj(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise InputError(f"invalid or incomplete triage {label}")
    return value


def utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        raise InputError("invalid triage Unicode text") from None


def path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or utf8_size(value) > 4096
        or value.startswith("/")
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or len(value.split("/")) > 64
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise InputError("invalid triage repository-relative path")
    return value


def timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        raise InputError("invalid triage review timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InputError("invalid triage review timestamp") from None
    if parsed.tzinfo is None:
        raise InputError("invalid triage review timestamp")
    return parsed


@dataclass(frozen=True, slots=True)
class TriageSnapshot:
    host: str
    repository: str
    repository_id: int
    number: int
    state: str
    draft: bool
    head_sha: str
    base_sha: str
    base_ref: str
    head_tree: str
    head_repository_id: int
    head_repository_fork: bool
    author_id: int
    author_login: str
    author_type: str
    files: tuple[dict[str, Any], ...]
    reviews: tuple[dict[str, Any], ...]
    candidates: tuple[dict[str, Any], ...]
    candidate_limit: int
    candidates_examined: int

    def boundary(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in (
                "host",
                "repository",
                "repository_id",
                "number",
                "state",
                "draft",
                "head_sha",
                "base_sha",
                "base_ref",
                "head_tree",
                "head_repository_id",
                "head_repository_fork",
                "author_id",
                "author_login",
                "author_type",
            )
        }


def parse_snapshot(value: object) -> TriageSnapshot:
    document = obj(
        value,
        {
            "version",
            "host",
            "repository",
            "repository_id",
            "number",
            "state",
            "draft",
            "head_sha",
            "base_sha",
            "base_ref",
            "head_tree",
            "head_repository_id",
            "head_repository_fork",
            "author_id",
            "author_login",
            "author_type",
            "files",
            "changed_files",
            "reviews",
            "candidates",
            "candidate_limit",
            "candidates_examined",
        },
        "snapshot",
    )
    if type(document["version"]) is not int or document["version"] != 1:
        raise InputError("triage snapshot version must be integer 1")
    host = document["host"]
    if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", host):
        raise InputError("invalid triage host")
    if not isinstance(document["repository"], str) or not _REPOSITORY.fullmatch(
        document["repository"]
    ):
        raise InputError("invalid triage repository name")
    for key in ("repository_id", "number", "head_repository_id", "author_id"):
        positive(document[key], key)
    for key in ("head_sha", "base_sha", "head_tree"):
        sha(document[key], key)
    if not isinstance(document["state"], str) or document["state"] not in {"open", "closed"}:
        raise InputError("invalid triage PR state")
    if type(document["draft"]) is not bool or type(document["head_repository_fork"]) is not bool:
        raise InputError("invalid triage draft or repository fork flag")
    if (
        document["head_repository_id"] != document["repository_id"]
        and not document["head_repository_fork"]
    ):
        raise InputError("inconsistent triage cross-repository provenance")
    base_ref = document["base_ref"]
    if (
        not isinstance(base_ref, str)
        or not 1 <= len(base_ref) <= 244
        or any(ord(c) < 33 for c in base_ref)
    ):
        raise InputError("invalid triage base branch")
    if not isinstance(document["author_login"], str) or not _LOGIN.fullmatch(
        document["author_login"]
    ):
        raise InputError("invalid triage author login")
    if not isinstance(document["author_type"], str) or document["author_type"] not in {
        "User",
        "Bot",
    }:
        raise InputError("invalid triage author type")
    files = document["files"]
    if (
        not isinstance(files, list)
        or len(files) > MAX_FILES
        or type(document["changed_files"]) is not int
        or document["changed_files"] != len(files)
    ):
        raise InputError("triage changed-file inventory is incomplete or over budget")
    seen = set()
    for item in files:
        item = obj(item, {"filename", "previous_filename", "sha", "status"}, "changed file")
        name = path(item["filename"])
        if name in seen:
            raise InputError("triage changed-file inventory contains duplicate paths")
        seen.add(name)
        sha(item["sha"], "file SHA")
        if not isinstance(item["status"], str) or item["status"] not in {
            "added",
            "removed",
            "modified",
            "renamed",
            "copied",
            "changed",
        }:
            raise InputError("invalid triage changed-file status")
        if item["status"] in {"renamed", "copied"}:
            path(item["previous_filename"])
        elif item["previous_filename"] is not None:
            raise InputError("unexpected triage previous filename")
    reviews = document["reviews"]
    if not isinstance(reviews, list) or len(reviews) > MAX_REVIEWS:
        raise InputError("triage reviews are incomplete or over budget")
    seen = set()
    for review in reviews:
        review = obj(
            review,
            {"id", "user_id", "user_type", "state", "commit_id", "submitted_at", "body"},
            "review",
        )
        if not isinstance(review["body"], str) or utf8_size(review["body"]) > 64 * 1024:
            raise InputError("invalid or oversized triage review body")
        review_id = positive(review["id"], "review ID")
        positive(review["user_id"], "review author ID")
        if (
            review_id in seen
            or not isinstance(review["user_type"], str)
            or review["user_type"] not in {"User", "Bot"}
        ):
            raise InputError("ambiguous triage review identity")
        seen.add(review_id)
        if not isinstance(review["state"], str) or review["state"] not in {
            "APPROVED",
            "CHANGES_REQUESTED",
            "DISMISSED",
            "COMMENTED",
            "PENDING",
        }:
            raise InputError("invalid triage review state")
        if review["commit_id"] is not None:
            sha(review["commit_id"], "review commit")
        if not (review["state"] == "PENDING" and review["submitted_at"] is None):
            timestamp(review["submitted_at"])
    limit = document["candidate_limit"]
    examined = document["candidates_examined"]
    candidates = document["candidates"]
    if (
        type(limit) is not int
        or not 0 <= limit <= 20
        or type(examined) is not int
        or not 0 <= examined <= limit
    ):
        raise InputError("invalid triage duplicate candidate scope")
    if not isinstance(candidates, list) or len(candidates) > examined:
        raise InputError("incomplete triage duplicate candidate inventory")
    seen = set()
    for candidate in candidates:
        candidate = obj(candidate, {"number", "merge_commit_sha", "tree_sha"}, "merged candidate")
        number = positive(candidate["number"], "merged PR number")
        if number == document["number"] or number in seen:
            raise InputError("duplicate triage merged PR candidate")
        seen.add(number)
        sha(candidate["merge_commit_sha"], "merged commit")
        sha(candidate["tree_sha"], "merged tree")
    return TriageSnapshot(
        **{
            key: document[key]
            for key in TriageSnapshot.__dataclass_fields__
            if key not in {"files", "reviews", "candidates"}
        },
        files=tuple(files),
        reviews=tuple(reviews),
        candidates=tuple(candidates),
    )


def acceptance_marker(policy: TriagePolicy, snapshot: TriageSnapshot) -> str:
    """Bind an explicit maintainer review to the full evaluated policy boundary."""
    boundary = {
        key: snapshot.boundary()[key]
        for key in ("host", "repository_id", "number", "head_sha", "base_sha", "base_ref")
    }
    boundary["policy_digest"] = policy.digest
    digest = hashlib.sha256(
        json.dumps(boundary, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{ACCEPTANCE_PREFIX}{digest} -->"


def evaluate(policy: TriagePolicy, snapshot: TriageSnapshot) -> dict[str, Any]:
    if snapshot.host != policy.host or snapshot.repository_id != policy.repository_id:
        raise InputError("triage policy host/repository identity does not match PR")
    if snapshot.candidate_limit != policy.duplicate_candidates:
        raise InputError("triage duplicate inventory does not match policy scope")
    spent = 0
    cache = {}

    def spend(units: int) -> None:
        nonlocal spent
        spent += units
        if spent > 10_000_000:
            raise InputError("triage path matching budget exhausted")

    def matches(pattern: str, filename: str) -> bool:
        if pattern not in cache:
            cache[pattern] = _compile_components(validate_pattern(pattern))
        return match_compiled(cache[pattern], tuple(filename.split("/")), spend_work=spend)

    findings = []
    for item in snapshot.files:
        for filename in dict.fromkeys(filter(None, (item["filename"], item["previous_filename"]))):
            if any(matches(pattern, filename) for pattern in policy.sensitive_patterns):
                findings.append({"code": "triage.sensitive-path", "path": filename})
    for candidate in snapshot.candidates:
        if candidate["number"] != snapshot.number and candidate["tree_sha"] == snapshot.head_tree:
            findings.append(
                {
                    "code": "triage.merged-tree-match",
                    "pull_request_number": candidate["number"],
                    "merge_commit_sha": candidate["merge_commit_sha"],
                }
            )
    same_repo = snapshot.head_repository_id == snapshot.repository_id
    classification = "external"
    if snapshot.author_type == "User" and snapshot.author_id in policy.maintainer_user_ids:
        classification = "configured-maintainer"
    matched_bot = next(
        (
            bot
            for bot in policy.trusted_bots
            if bot.user_id == snapshot.author_id
            and bot.login == snapshot.author_login
            and snapshot.author_type == "Bot"
        ),
        None,
    )
    if matched_bot:
        classification = "configured-automation"
    latest = {}
    for review in snapshot.reviews:
        if (
            review["user_type"] == "User"
            and review["user_id"] in policy.maintainer_user_ids
            and review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
        ):
            key = review["user_id"]
            order = (timestamp(review["submitted_at"]), review["id"])
            if key not in latest or order > (
                timestamp(latest[key]["submitted_at"]),
                latest[key]["id"],
            ):
                latest[key] = review
    requested_changes = any(review["state"] == "CHANGES_REQUESTED" for review in latest.values())
    current_approvals = [
        review
        for review in latest.values()
        if review["state"] == "APPROVED"
        and review["commit_id"] == snapshot.head_sha
        and review["user_id"] != snapshot.author_id
    ]
    acknowledgment = acceptance_marker(policy, snapshot)
    approvals = [
        review
        for review in current_approvals
        if review["body"].count(ACCEPTANCE_PREFIX) == 1
        and acknowledgment in review["body"].splitlines()
    ]
    approval = max(
        approvals,
        key=lambda review: (timestamp(review["submitted_at"]), review["id"]),
        default=None,
    )
    bot_scope = (
        bool(matched_bot)
        and not findings
        and all(
            any(matches(pattern, item["filename"]) for pattern in matched_bot.allowed_patterns)
            and item["previous_filename"] is None
            for item in snapshot.files
        )
    )
    automatic = same_repo and (classification == "configured-maintainer" or bot_scope)
    allowed = (
        snapshot.state == "open"
        and not snapshot.draft
        and not requested_changes
        and (automatic or approval is not None)
    )
    reason = (
        "maintainer-review"
        if approval
        else "trusted-source"
        if automatic
        else "maintainer-review-required"
    )
    if requested_changes:
        reason = "maintainer-changes-requested"
    elif not allowed and current_approvals:
        reason = "maintainer-acknowledgment-required"
    if snapshot.state != "open" or snapshot.draft:
        reason = "not-open-ready"
    return {
        "schema_version": 1,
        "kind": "pull_request_triage",
        "policy_digest": policy.digest,
        "boundary": snapshot.boundary(),
        "classification": classification,
        "provenance": "same-repository" if same_repo else "fork",
        "inventory": {
            "complete": True,
            "changed_files": len(snapshot.files),
            "content_analysis": False,
            "duplicate_scope": "most-recently-updated-closed-pull-requests",
            "candidate_limit": snapshot.candidate_limit,
            "candidates_examined": snapshot.candidates_examined,
            "merged_candidates": len(snapshot.candidates),
        },
        "admission": {
            "decision": "allow" if allowed else "hold",
            "reason": reason,
            "review_id": approval["id"] if approval else None,
            "reviewer_id": approval["user_id"] if approval else None,
            "head_sha": snapshot.head_sha,
            "acknowledgment": acknowledgment,
        },
        "finding_count": len(findings),
        "omitted_findings": max(0, len(findings) - MAX_FINDINGS),
        "findings": findings[:MAX_FINDINGS],
    }


def decode_snapshot(raw: bytes) -> TriageSnapshot:
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise InputError("triage snapshot exceeds 1 MiB")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InputError("triage snapshot contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                InputError("invalid triage JSON constant")
            ),
        )
    except (ValueError, RecursionError):
        raise InputError("invalid triage snapshot JSON") from None
    return parse_snapshot(document)
