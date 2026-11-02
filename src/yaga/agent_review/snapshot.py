"""Strict, bounded pull-request review snapshot parsing and evaluation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yaga.agent_review.evaluation import LensOutcome, ReviewAggregate
from yaga.agent_review.plan import build_plan
from yaga.agent_review.policy import AgentReviewPolicy, ReviewLens
from yaga.agent_review.results import AgentReviewResults, LensResult, evaluate_results
from yaga.errors import ConfigurationError, InputError
from yaga.files import read_file_prefix

MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_SNAPSHOT_RECORDS = 100
MAX_SNAPSHOT_BODY_BYTES = 65536
MAX_PATH_BYTES = 4096
MAX_URL_BYTES = 1024
MAX_CHECK_RUN_NAME_BYTES = 256
MAX_APP_SLUG_BYTES = 128
MAX_PORTABLE_INT = (1 << 53) - 1

_SHA = re.compile(r"^[0-9a-f]{40}$")
_PLAN_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?$")
_REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"})
_PR_STATES = frozenset({"open", "closed"})
_REACTIONS = frozenset({"+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"})
_CHECK_RUN_STATUSES = frozenset(
    {"queued", "in_progress", "completed", "waiting", "requested", "pending"}
)
_CHECK_RUN_CONCLUSIONS = frozenset(
    {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "stale",
    }
)
_CODEX_FINDING_BADGE = re.compile(r"!\[P[0-3]\s+Badge\]", re.IGNORECASE)
_CLEAN_CODEX_COMMENT = re.compile(
    r"(?:agent|codex)\s+review:\s*didn['\u2019]t\s+find\s+any\s+major\s+issues\.",
    re.IGNORECASE,
)
_REVIEWED_COMMIT_RE = re.compile(
    r"(?m)^\*\*Reviewed commit:\*\*\s*`(?P<commit>[0-9a-f]{7,40})`\s*$",
    re.IGNORECASE,
)
_CODEX_REVIEW_STATE_RE = re.compile(
    r"<!--\s*codex-review-state:v1\s+(\{.*?\})\s*-->",
    re.DOTALL,
)

_ROOT_KEYS = frozenset(
    {
        "version",
        "plan_digest",
        "number",
        "url",
        "state",
        "draft",
        "head_sha",
        "base_sha",
        "head_committed_at",
        "reviews",
        "review_comments",
        "issue_comments",
        "reactions",
        "check_runs",
    }
)
_REVIEW_KEYS = frozenset(
    {"id", "author_login", "author_id", "state", "commit_id", "submitted_at", "body"}
)
_REVIEW_COMMENT_KEYS = frozenset(
    {
        "id",
        "pull_request_review_id",
        "author_login",
        "author_id",
        "commit_id",
        "created_at",
        "path",
        "body",
    }
)
_ISSUE_COMMENT_KEYS = frozenset(
    {"id", "author_login", "author_id", "app_id", "created_at", "updated_at", "body"}
)
_REACTION_KEYS = frozenset({"id", "author_login", "author_id", "content", "created_at"})
_CHECK_RUN_KEYS = frozenset(
    {
        "id",
        "name",
        "status",
        "conclusion",
        "head_sha",
        "app_slug",
        "app_id",
        "started_at",
        "completed_at",
    }
)


@dataclass(frozen=True, slots=True)
class PullRequestReviewRecord:
    """One pull-request review entry from a snapshot."""

    review_id: int
    author_login: str
    author_id: int | None
    state: str
    commit_id: str | None
    submitted_at: datetime | None
    body: str = ""


@dataclass(frozen=True, slots=True)
class PullRequestReviewCommentRecord:
    """One inline pull-request review comment entry from a snapshot."""

    comment_id: int
    pull_request_review_id: int | None
    author_login: str
    author_id: int | None
    commit_id: str | None
    created_at: datetime
    path: str
    body: str = ""


@dataclass(frozen=True, slots=True)
class PullRequestIssueCommentRecord:
    """One pull-request conversation comment entry from a snapshot."""

    comment_id: int
    author_login: str
    author_id: int | None
    app_id: int | None
    created_at: datetime
    body: str = ""
    updated_at: datetime | None = None

    @property
    def effective_at(self) -> datetime:
        """Return updated_at when present and not earlier than created_at."""
        if self.updated_at is not None and self.updated_at >= self.created_at:
            return self.updated_at
        return self.created_at


@dataclass(frozen=True, slots=True)
class PullRequestReactionRecord:
    """One pull-request body reaction entry from a snapshot."""

    reaction_id: int
    author_login: str
    author_id: int | None
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PullRequestCheckRunRecord:
    """One head-commit check-run entry from a snapshot."""

    check_run_id: int
    name: str
    status: str
    conclusion: str | None = None
    head_sha: str | None = None
    app_slug: str | None = None
    app_id: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PullRequestReviewSnapshot:
    """Validated, bounded snapshot of one pull request's review state."""

    number: int
    head_sha: str
    base_sha: str
    state: str = "open"
    draft: bool = False
    url: str | None = None
    head_committed_at: datetime | None = None
    reviews: tuple[PullRequestReviewRecord, ...] = ()
    review_comments: tuple[PullRequestReviewCommentRecord, ...] = ()
    issue_comments: tuple[PullRequestIssueCommentRecord, ...] = ()
    reactions: tuple[PullRequestReactionRecord, ...] = ()
    check_runs: tuple[PullRequestCheckRunRecord, ...] = ()
    plan_digest: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotEvaluation:
    """Result receipt and aggregate outcome for one evaluated PR review snapshot."""

    snapshot: PullRequestReviewSnapshot
    results: AgentReviewResults
    aggregate: ReviewAggregate


@dataclass(frozen=True, slots=True)
class _CodexCommentState:
    """Parsed commit-bound state from a Codex summary issue comment."""

    commit_sha: str
    outcome: LensOutcome
    effective_at: datetime
    comment_id: int


def load_pull_request_snapshot(path: Path) -> PullRequestReviewSnapshot:
    """Load one explicit JSON snapshot of pull-request review state."""
    resolved = path.expanduser().resolve()
    try:
        raw = read_file_prefix(resolved, maximum=MAX_SNAPSHOT_BYTES)
    except OSError as error:
        raise InputError(f"cannot read pull-request review snapshot {resolved}: {error}") from error
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise InputError(
            f"pull-request review snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes: {resolved}"
        )
    try:
        document = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise InputError(f"pull-request review snapshot is not valid JSON: {resolved}") from error
    return parse_pull_request_snapshot(document, source=str(resolved))


def parse_pull_request_snapshot(
    document: Any,
    *,
    source: str = "snapshot",
) -> PullRequestReviewSnapshot:
    """Validate a decoded JSON object into a bounded PullRequestReviewSnapshot."""
    if not isinstance(document, dict):
        raise InputError(f"pull-request review snapshot must be an object in {source}")
    _reject_unknown(document, _ROOT_KEYS, "pull-request review snapshot", source)
    version = document.get("version")
    if type(version) is not int or version != 1:
        raise InputError(f"pull-request review snapshot version must be integer 1 in {source}")
    plan_digest = _optional_plan_digest(document.get("plan_digest"), source)
    number = _positive_int(document.get("number"), "number", source)
    head_sha = _sha(document.get("head_sha"), "head_sha", source)
    base_sha = _sha(document.get("base_sha"), "base_sha", source)
    state = document.get("state", "open")
    if not isinstance(state, str) or state not in _PR_STATES:
        raise InputError(f"pull-request review snapshot state is invalid in {source}")
    draft = document.get("draft", False)
    if type(draft) is not bool:
        raise InputError(f"pull-request review snapshot draft must be a boolean in {source}")
    url = _optional_text(document.get("url"), "url", source, maximum=MAX_URL_BYTES)
    head_committed_at = _optional_timestamp(
        document.get("head_committed_at"), "head_committed_at", source
    )
    reviews = _parse_reviews(document.get("reviews", []), source)
    review_comments = _parse_review_comments(document.get("review_comments", []), source)
    issue_comments = _parse_issue_comments(document.get("issue_comments", []), source)
    reactions = _parse_reactions(document.get("reactions", []), source)
    check_runs = _parse_check_runs(document.get("check_runs", []), source)
    return PullRequestReviewSnapshot(
        number=number,
        head_sha=head_sha,
        base_sha=base_sha,
        state=state,
        draft=draft,
        url=url,
        head_committed_at=head_committed_at,
        reviews=reviews,
        review_comments=review_comments,
        issue_comments=issue_comments,
        reactions=reactions,
        check_runs=check_runs,
        plan_digest=plan_digest,
    )


def evaluate_pull_request_snapshot(
    policy: AgentReviewPolicy,
    snapshot: PullRequestReviewSnapshot,
) -> SnapshotEvaluation:
    """Evaluate every configured policy lens against one PR review snapshot."""
    if not isinstance(policy, AgentReviewPolicy):
        raise TypeError("policy must be an AgentReviewPolicy")
    if not isinstance(snapshot, PullRequestReviewSnapshot):
        raise TypeError("snapshot must be a PullRequestReviewSnapshot")
    plan = build_plan(policy)
    expected_digest = plan.digest()
    if snapshot.plan_digest != expected_digest:
        raise ConfigurationError(
            "pull-request review snapshot plan_digest does not match the current policy plan"
        )
    seen_bots: dict[str, str] = {}
    for lens in policy.lenses:
        bot = lens.resolve_bot()
        bot_key = bot.bot_login.casefold()
        if bot_key in seen_bots:
            raise ConfigurationError(
                f"Agent review lenses {seen_bots[bot_key]} and {lens.name} resolve to "
                f"the same bot-login ({bot.bot_login}); pull-request snapshot evaluation "
                "requires distinct bot identities per lens"
            )
        seen_bots[bot_key] = lens.name
    lens_results = tuple(_evaluate_lens(lens, snapshot) for lens in policy.lenses)
    results = AgentReviewResults(
        version=policy.version,
        plan_digest=expected_digest,
        results=lens_results,
    )
    aggregate = evaluate_results(policy, results)
    return SnapshotEvaluation(snapshot=snapshot, results=results, aggregate=aggregate)


def _evaluate_lens(lens: ReviewLens, snapshot: PullRequestReviewSnapshot) -> LensResult:
    bot = lens.resolve_bot()
    bot_key = bot.bot_login.casefold()
    short_head = snapshot.head_sha[:12]

    matched_reviews: list[PullRequestReviewRecord] = []
    for review in snapshot.reviews:
        if review.author_login.casefold() != bot_key:
            continue
        _verify_user_id(review.author_id, bot.bot_user_id, bot.bot_login, "review")
        if review.state in {"DISMISSED", "PENDING"}:
            continue
        if review.commit_id is None or review.submitted_at is None:
            raise InputError(
                f"review {review.review_id} from {bot.bot_login} is missing "
                "commit_id or submitted_at"
            )
        body_commit = _extract_reviewed_commit_prefix(
            review.body, label=f"review {review.review_id}"
        )
        if body_commit is not None and not review.commit_id.startswith(body_commit):
            raise InputError(
                f"review {review.review_id} from {bot.bot_login} has inconsistent "
                "Reviewed commit marker"
            )
        matched_reviews.append(review)

    current_reviews: list[PullRequestReviewRecord] = []
    stale_reviews: list[PullRequestReviewRecord] = []
    for review in matched_reviews:
        assert review.submitted_at is not None
        if review.commit_id == snapshot.head_sha and (
            snapshot.head_committed_at is None or review.submitted_at >= snapshot.head_committed_at
        ):
            current_reviews.append(review)
        else:
            stale_reviews.append(review)

    current_review_ids = {review.review_id for review in current_reviews}
    stale_review_ids = {review.review_id for review in stale_reviews}

    current_inline_comments: list[PullRequestReviewCommentRecord] = []
    for comment in snapshot.review_comments:
        if comment.author_login.casefold() != bot_key:
            continue
        _verify_user_id(comment.author_id, bot.bot_user_id, bot.bot_login, "review comment")
        if comment.commit_id is not None:
            if comment.commit_id == snapshot.head_sha and (
                snapshot.head_committed_at is None
                or comment.created_at >= snapshot.head_committed_at
            ):
                current_inline_comments.append(comment)
        elif comment.pull_request_review_id in current_review_ids:
            current_inline_comments.append(comment)
        elif comment.pull_request_review_id not in stale_review_ids:
            raise InputError(
                f"review comment {comment.comment_id} from {bot.bot_login} has no commit binding"
            )

    matched_issue_comments: list[PullRequestIssueCommentRecord] = []
    commit_bound_issue_comments: list[PullRequestIssueCommentRecord] = []
    current_codex_states: list[_CodexCommentState] = []
    stale_codex_states: list[_CodexCommentState] = []
    for issue_comment in snapshot.issue_comments:
        if issue_comment.author_login.casefold() != bot_key:
            continue
        _verify_user_id(issue_comment.author_id, bot.bot_user_id, bot.bot_login, "issue comment")
        if bot.bot_app_id is not None and issue_comment.app_id != bot.bot_app_id:
            raise InputError(
                f"issue comment {issue_comment.comment_id} from {bot.bot_login} "
                "has an unexpected app_id"
            )
        codex_state = _parse_codex_comment_state(issue_comment, head_sha=snapshot.head_sha)
        if codex_state is not None:
            if codex_state.commit_sha == snapshot.head_sha and (
                snapshot.head_committed_at is None
                or codex_state.effective_at >= snapshot.head_committed_at
            ):
                current_codex_states.append(codex_state)
            else:
                stale_codex_states.append(codex_state)
            continue
        is_clean = _CLEAN_CODEX_COMMENT.search(issue_comment.body) is not None
        if (
            is_clean
            and issue_comment.updated_at is not None
            and issue_comment.updated_at != issue_comment.created_at
        ):
            raise InputError(
                f"issue comment {issue_comment.comment_id} from {bot.bot_login} "
                "was edited and cannot be trusted as a clean review outcome"
            )
        reviewed_prefix = _extract_reviewed_commit_prefix(
            issue_comment.body, label=f"issue comment {issue_comment.comment_id}"
        )
        if reviewed_prefix is not None:
            if not snapshot.head_sha.startswith(reviewed_prefix):
                stale_codex_states.append(
                    _CodexCommentState(
                        commit_sha=reviewed_prefix,
                        outcome=LensOutcome.PASSED if is_clean else LensOutcome.FAILED,
                        effective_at=issue_comment.created_at,
                        comment_id=issue_comment.comment_id,
                    )
                )
                continue
            if any(
                (review.commit_id or "").startswith(reviewed_prefix) for review in stale_reviews
            ):
                raise InputError(
                    f"issue comment {issue_comment.comment_id} from {bot.bot_login} "
                    "has an ambiguous Reviewed commit prefix"
                )
            if (
                snapshot.head_committed_at is not None
                and issue_comment.created_at < snapshot.head_committed_at
            ):
                stale_codex_states.append(
                    _CodexCommentState(
                        commit_sha=reviewed_prefix,
                        outcome=LensOutcome.PASSED if is_clean else LensOutcome.FAILED,
                        effective_at=issue_comment.created_at,
                        comment_id=issue_comment.comment_id,
                    )
                )
                continue
            commit_bound_issue_comments.append(issue_comment)
            continue
        matched_issue_comments.append(issue_comment)

    matched_reactions: list[PullRequestReactionRecord] = []
    for reaction in snapshot.reactions:
        if reaction.author_login.casefold() != bot_key:
            continue
        _verify_user_id(reaction.author_id, bot.bot_user_id, bot.bot_login, "reaction")
        matched_reactions.append(reaction)

    failed_check_runs: list[tuple[str, str]] = []
    in_progress_check_runs: list[tuple[str, str]] = []
    missing_check_runs: list[str] = []
    passed_check_runs: list[tuple[str, PullRequestCheckRunRecord]] = []
    stale_check_runs: list[PullRequestCheckRunRecord] = []
    if bot.check_runs:
        configured_names = set(bot.check_runs)
        current_by_name: dict[str, list[PullRequestCheckRunRecord]] = {
            name: [] for name in bot.check_runs
        }
        for check_run in snapshot.check_runs:
            if check_run.name not in configured_names:
                continue
            if bot.bot_app_id is not None and check_run.app_id != bot.bot_app_id:
                raise InputError(
                    f"check-run {check_run.check_run_id} ({check_run.name}) "
                    f"for {bot.bot_login} has an unexpected app_id"
                )
            if check_run.head_sha is None or _SHA.fullmatch(check_run.head_sha) is None:
                raise InputError(
                    f"check-run {check_run.check_run_id} ({check_run.name}) "
                    f"for {bot.bot_login} is missing a valid head_sha"
                )
            if check_run.head_sha != snapshot.head_sha:
                stale_check_runs.append(check_run)
                continue
            effective_run_time = check_run.completed_at or check_run.started_at
            if (
                snapshot.head_committed_at is not None
                and effective_run_time is not None
                and effective_run_time < snapshot.head_committed_at
            ):
                stale_check_runs.append(check_run)
                continue
            current_by_name[check_run.name].append(check_run)

        for run_name in bot.check_runs:
            runs = current_by_name[run_name]
            if not runs:
                missing_check_runs.append(run_name)
                continue
            latest_run = _select_latest_check_run(runs)
            if latest_run.status != "completed":
                in_progress_check_runs.append((run_name, latest_run.status))
            elif latest_run.conclusion != "success":
                assert latest_run.conclusion is not None
                failed_check_runs.append((run_name, latest_run.conclusion))
            else:
                passed_check_runs.append((run_name, latest_run))

    # Non-commit-bound signals (ordinary issue comments without a Reviewed
    # commit marker and PR-body reactions) have no commit_id of their own and
    # therefore ALWAYS require snapshot.head_committed_at to prove freshness,
    # using created_at (never updated_at) so edits cannot promote stale comments.
    latest_stale_time = max(
        [
            *(review.submitted_at for review in stale_reviews if review.submitted_at is not None),
            *(state.effective_at for state in stale_codex_states),
        ],
        default=None,
    )
    current_issue_comments: list[PullRequestIssueCommentRecord] = [
        comment
        for comment in commit_bound_issue_comments
        if latest_stale_time is None or comment.created_at > latest_stale_time
    ]
    for comment in commit_bound_issue_comments:
        if latest_stale_time is not None and comment.created_at <= latest_stale_time:
            stale_codex_states.append(
                _CodexCommentState(
                    commit_sha=snapshot.head_sha,
                    outcome=(
                        LensOutcome.PASSED
                        if _CLEAN_CODEX_COMMENT.search(comment.body) is not None
                        else LensOutcome.FAILED
                    ),
                    effective_at=comment.created_at,
                    comment_id=comment.comment_id,
                )
            )
    current_reactions: list[PullRequestReactionRecord] = []
    stale_reactions: list[PullRequestReactionRecord] = []
    if snapshot.head_committed_at is None:
        for issue_comment in matched_issue_comments:
            if _CLEAN_CODEX_COMMENT.search(issue_comment.body) is None:
                current_issue_comments.append(issue_comment)
        stale_reactions.extend(
            reaction
            for reaction in matched_reactions
            if reaction.content in set(bot.pass_reactions) | set(bot.pending_reactions)
        )
    else:
        for issue_comment in matched_issue_comments:
            if issue_comment.created_at <= snapshot.head_committed_at:
                continue
            if latest_stale_time is not None and issue_comment.created_at <= latest_stale_time:
                continue
            current_issue_comments.append(issue_comment)

        for reaction in matched_reactions:
            is_after_head = reaction.created_at > snapshot.head_committed_at
            is_after_stale = latest_stale_time is None or reaction.created_at > latest_stale_time
            if is_after_head and is_after_stale:
                current_reactions.append(reaction)
            elif reaction.content in set(bot.pass_reactions) | set(bot.pending_reactions):
                stale_reactions.append(reaction)

    pass_matches = [
        reaction for reaction in current_reactions if reaction.content in bot.pass_reactions
    ]
    pending_matches = [
        reaction for reaction in current_reactions if reaction.content in bot.pending_reactions
    ]
    latest_pending = (
        max(pending_matches, key=lambda item: (item.created_at, item.reaction_id))
        if pending_matches
        else None
    )

    current_reviews.sort(
        key=lambda item: (item.submitted_at or datetime.min.replace(tzinfo=UTC), item.review_id)
    )
    latest_approval_time = max(
        (
            review.submitted_at
            for review in current_reviews
            if review.state == "APPROVED" and review.submitted_at is not None
        ),
        default=None,
    )
    latest_changes_requested_time = max(
        (
            review.submitted_at
            for review in current_reviews
            if review.state == "CHANGES_REQUESTED" and review.submitted_at is not None
        ),
        default=None,
    )
    has_active_approval = latest_approval_time is not None and (
        latest_changes_requested_time is None
        or latest_approval_time > latest_changes_requested_time
    )

    codex_failed_times = [
        state.effective_at for state in current_codex_states if state.outcome is LensOutcome.FAILED
    ]
    latest_codex_failed_time = max(codex_failed_times, default=None)
    codex_passed_times = [
        state.effective_at for state in current_codex_states if state.outcome is LensOutcome.PASSED
    ]
    latest_codex_passed_time = max(codex_passed_times, default=None)
    has_active_codex_resolution = (
        latest_codex_passed_time is not None
        and (
            latest_codex_failed_time is None or latest_codex_passed_time > latest_codex_failed_time
        )
        and (
            latest_changes_requested_time is None
            or latest_codex_passed_time > latest_changes_requested_time
        )
    )

    review_times_by_id = {
        review.review_id: review.submitted_at
        for review in current_reviews
        if review.submitted_at is not None
    }
    review_ids_with_inline_comments = {
        comment.pull_request_review_id
        for comment in current_inline_comments
        if comment.pull_request_review_id is not None
    }

    if current_inline_comments:
        latest_inline_time = max(
            max(
                comment.created_at,
                review_times_by_id.get(comment.pull_request_review_id or -1, comment.created_at),
            )
            for comment in current_inline_comments
        )
        cleared_by_approval = has_active_approval and latest_approval_time > latest_inline_time
        cleared_by_summary = (
            has_active_codex_resolution and latest_codex_passed_time >= latest_inline_time
        )
        if not (cleared_by_approval or cleared_by_summary):
            return LensResult(
                lens=lens.name,
                outcome=LensOutcome.FAILED,
                summary=(
                    f"{bot.bot_login} reported {len(current_inline_comments)} inline finding(s) "
                    f"on head {short_head}"
                ),
            )

    if latest_changes_requested_time is not None and not has_active_approval:
        return LensResult(
            lens=lens.name,
            outcome=LensOutcome.FAILED,
            summary=f"{bot.bot_login} requested changes on head {short_head}",
        )

    badge_finding_times = [
        review.submitted_at
        for review in current_reviews
        if review.state == "COMMENTED"
        and review.submitted_at is not None
        and _CODEX_FINDING_BADGE.search(review.body) is not None
    ]
    if badge_finding_times:
        latest_badge_time = max(badge_finding_times)
        cleared_by_approval = has_active_approval and latest_approval_time > latest_badge_time
        cleared_by_summary = (
            has_active_codex_resolution and latest_codex_passed_time >= latest_badge_time
        )
        if not (cleared_by_approval or cleared_by_summary):
            return LensResult(
                lens=lens.name,
                outcome=LensOutcome.FAILED,
                summary=f"{bot.bot_login} reported review findings on head {short_head}",
            )

    if (
        latest_codex_failed_time is not None
        and not (has_active_approval and latest_approval_time > latest_codex_failed_time)
        and not has_active_codex_resolution
    ):
        return LensResult(
            lens=lens.name,
            outcome=LensOutcome.FAILED,
            summary=f"{bot.bot_login} reported unresolved findings on head {short_head}",
        )

    non_clean_issue_comments = [
        comment
        for comment in current_issue_comments
        if _CLEAN_CODEX_COMMENT.search(comment.body) is None
    ]
    if non_clean_issue_comments:
        return LensResult(
            lens=lens.name,
            outcome=LensOutcome.FAILED,
            summary=f"{bot.bot_login} posted a non-clean issue comment on head {short_head}",
        )

    if failed_check_runs:
        run_name, conclusion = failed_check_runs[0]
        return LensResult(
            lens=lens.name,
            outcome=LensOutcome.FAILED,
            summary=(
                f"check-run {run_name} for {bot.bot_login} completed with {conclusion} "
                f"on head {short_head}"
            ),
        )

    if in_progress_check_runs:
        run_name, status = in_progress_check_runs[0]
        return LensResult(
            lens=lens.name,
            outcome=LensOutcome.PENDING,
            summary=f"check-run {run_name} for {bot.bot_login} is {status} for head {short_head}",
        )

    if missing_check_runs:
        run_name = missing_check_runs[0]
        if any(item.name == run_name for item in stale_check_runs):
            return LensResult(
                lens=lens.name,
                outcome=LensOutcome.PENDING,
                summary=(
                    f"last check-run {run_name} for {bot.bot_login} is stale; "
                    f"waiting for head {short_head}"
                ),
            )
        return LensResult(
            lens=lens.name,
            outcome=LensOutcome.PENDING,
            summary=f"waiting for check-run {run_name} for {bot.bot_login} on head {short_head}",
        )

    codex_pending_times = [
        state.effective_at for state in current_codex_states if state.outcome is LensOutcome.PENDING
    ]
    latest_codex_pending_time = max(codex_pending_times, default=None)
    codex_passed_states = [
        state for state in current_codex_states if state.outcome is LensOutcome.PASSED
    ]
    latest_codex_passed = (
        max(codex_passed_states, key=lambda item: (item.effective_at, item.comment_id))
        if codex_passed_states
        else None
    )
    clean_reviews = [
        review
        for review in current_reviews
        if review.state == "APPROVED"
        or (
            review.state == "COMMENTED"
            and review.review_id not in review_ids_with_inline_comments
            and _CODEX_FINDING_BADGE.search(review.body) is None
        )
    ]
    latest_review = clean_reviews[-1] if clean_reviews else None
    latest_clean_comment = (
        max(current_issue_comments, key=lambda item: (item.created_at, item.comment_id))
        if current_issue_comments
        else None
    )
    latest_pass = (
        max(pass_matches, key=lambda item: (item.created_at, item.reaction_id))
        if pass_matches
        else None
    )
    completion_times: list[datetime] = []
    if latest_review is not None:
        assert latest_review.submitted_at is not None
        completion_times.append(latest_review.submitted_at)
    if latest_codex_passed is not None:
        completion_times.append(latest_codex_passed.effective_at)
    if latest_clean_comment is not None:
        completion_times.append(latest_clean_comment.created_at)
    if latest_pass is not None:
        completion_times.append(latest_pass.created_at)
    for _, check_run in passed_check_runs:
        run_time = check_run.completed_at or check_run.started_at
        if run_time is not None:
            completion_times.append(run_time)
    latest_completion_time = max(completion_times, default=None)

    if latest_completion_time is not None or passed_check_runs:
        pending_reaction_newer = latest_pending is not None and (
            latest_completion_time is None or latest_pending.created_at > latest_completion_time
        )
        pending_summary_newer = latest_codex_pending_time is not None and (
            latest_completion_time is None or latest_codex_pending_time > latest_completion_time
        )
        if pending_reaction_newer or pending_summary_newer:
            if pending_reaction_newer and (
                not pending_summary_newer
                or (
                    latest_pending is not None
                    and latest_codex_pending_time is not None
                    and latest_pending.created_at >= latest_codex_pending_time
                )
            ):
                assert latest_pending is not None
                return LensResult(
                    lens=lens.name,
                    outcome=LensOutcome.PENDING,
                    summary=(
                        f"{bot.bot_login} review is in progress "
                        f"(reaction: {latest_pending.content}) for head {short_head}"
                    ),
                )
            return LensResult(
                lens=lens.name,
                outcome=LensOutcome.PENDING,
                summary=f"{bot.bot_login} review is in progress for head {short_head}",
            )
        if latest_review is not None:
            if latest_review.state == "APPROVED":
                return LensResult(
                    lens=lens.name,
                    outcome=LensOutcome.PASSED,
                    summary=f"{bot.bot_login} approved head {short_head}",
                )
            return LensResult(
                lens=lens.name,
                outcome=LensOutcome.PASSED,
                summary=f"{bot.bot_login} reviewed head {short_head} with no findings",
            )
        if latest_codex_passed is not None:
            return LensResult(
                lens=lens.name,
                outcome=LensOutcome.PASSED,
                summary=f"{bot.bot_login} completed review with no findings on head {short_head}",
            )
        if latest_clean_comment is not None:
            return LensResult(
                lens=lens.name,
                outcome=LensOutcome.PASSED,
                summary=f"{bot.bot_login} reported no findings on head {short_head}",
            )
        if passed_check_runs:
            names = ", ".join(name for name, _ in passed_check_runs)
            return LensResult(
                lens=lens.name,
                outcome=LensOutcome.PASSED,
                summary=f"check-run {names} for {bot.bot_login} passed on head {short_head}",
            )
        assert latest_pass is not None
        return LensResult(
            lens=lens.name,
            outcome=LensOutcome.PASSED,
            summary=f"{bot.bot_login} reacted with {latest_pass.content} on head {short_head}",
        )

    if latest_codex_pending_time is not None:
        return LensResult(
            lens=lens.name,
            outcome=LensOutcome.PENDING,
            summary=f"{bot.bot_login} review is in progress for head {short_head}",
        )

    if latest_pending is not None:
        return LensResult(
            lens=lens.name,
            outcome=LensOutcome.PENDING,
            summary=(
                f"{bot.bot_login} review is in progress "
                f"(reaction: {latest_pending.content}) for head {short_head}"
            ),
        )

    if stale_reviews or stale_codex_states:
        stale_candidates: list[tuple[datetime, int, str]] = [
            (
                review.submitted_at or datetime.min.replace(tzinfo=UTC),
                review.review_id,
                review.commit_id or "unknown",
            )
            for review in stale_reviews
        ]
        stale_candidates.extend(
            (state.effective_at, state.comment_id, state.commit_sha) for state in stale_codex_states
        )
        latest_stale_sha = max(stale_candidates, key=lambda item: (item[0], item[1]))[2][:12]
        return LensResult(
            lens=lens.name,
            outcome=LensOutcome.PENDING,
            summary=(
                f"last review from {bot.bot_login} is stale (commit {latest_stale_sha}); "
                f"waiting for head {short_head}"
            ),
        )

    if stale_reactions:
        return LensResult(
            lens=lens.name,
            outcome=LensOutcome.PENDING,
            summary=(
                f"last reaction from {bot.bot_login} predates head {short_head}; "
                "waiting for current review"
            ),
        )

    return LensResult(
        lens=lens.name,
        outcome=LensOutcome.PENDING,
        summary=f"no review activity from {bot.bot_login} for head {short_head}",
    )


def _extract_reviewed_commit_prefix(body: str, *, label: str) -> str | None:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    matches = _REVIEWED_COMMIT_RE.findall(normalized)
    if not matches:
        return None
    if len(matches) != 1:
        raise InputError(f"{label} contains multiple Reviewed commit markers")
    return matches[0].lower()


def _parse_codex_comment_state(
    comment: PullRequestIssueCommentRecord,
    *,
    head_sha: str,
) -> _CodexCommentState | None:
    matches = _CODEX_REVIEW_STATE_RE.findall(comment.body)
    if not matches:
        return None
    if len(matches) != 1:
        raise InputError(
            f"issue comment {comment.comment_id} contains multiple codex-review-state markers"
        )
    try:
        payload = json.loads(
            matches[0],
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise InputError(
            f"issue comment {comment.comment_id} has invalid codex-review-state JSON"
        ) from error
    if not isinstance(payload, dict):
        raise InputError(f"issue comment {comment.comment_id} codex-review-state must be an object")
    active_sections: list[tuple[str, LensOutcome]] = []
    for section_name in ("code_review", "security_review"):
        section = payload.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise InputError(
                f"issue comment {comment.comment_id} {section_name} state must be an object"
            )
        commit_sha = section.get("commit_sha")
        status = section.get("status")
        has_any = section.get("has_any_findings")
        has_unresolved = section.get("has_unresolved_findings")
        if has_any is not None and not isinstance(has_any, bool):
            raise InputError(
                f"issue comment {comment.comment_id} {section_name}.has_any_findings "
                "must be a boolean or null"
            )
        if has_unresolved is not None and not isinstance(has_unresolved, bool):
            raise InputError(
                f"issue comment {comment.comment_id} {section_name}.has_unresolved_findings "
                "must be a boolean or null"
            )
        if commit_sha is None and status is None:
            if has_any is not None or has_unresolved is not None:
                raise InputError(
                    f"issue comment {comment.comment_id} {section_name} inactive state "
                    "cannot set finding flags"
                )
            continue
        if not isinstance(commit_sha, str) or _SHA.fullmatch(commit_sha) is None:
            raise InputError(
                f"issue comment {comment.comment_id} {section_name}.commit_sha is invalid"
            )
        if not isinstance(status, str) or status not in {
            "completed",
            "running",
            "in_progress",
            "queued",
            "pending",
        }:
            raise InputError(f"issue comment {comment.comment_id} {section_name}.status is invalid")
        if status != "completed":
            outcome = LensOutcome.PENDING
        elif has_any is False and has_unresolved is True:
            raise InputError(
                f"issue comment {comment.comment_id} {section_name} finding flags are contradictory"
            )
        elif has_unresolved is True or (has_unresolved is None and has_any is True):
            outcome = LensOutcome.FAILED
        elif has_any is False or has_unresolved is False:
            outcome = LensOutcome.PASSED
        elif section_name == "security_review" and has_any is None and has_unresolved is None:
            outcome = LensOutcome.PASSED
        else:
            raise InputError(
                f"issue comment {comment.comment_id} {section_name} finding flags are invalid"
            )
        active_sections.append((commit_sha, outcome))
    if not active_sections:
        raise InputError(
            f"issue comment {comment.comment_id} codex-review-state has no active review section"
        )
    current_outcomes = [outcome for sha, outcome in active_sections if sha == head_sha]
    if current_outcomes:
        target_sha = head_sha
        outcomes = set(current_outcomes)
    else:
        target_sha = active_sections[0][0]
        outcomes = {outcome for _, outcome in active_sections}
    if LensOutcome.FAILED in outcomes:
        aggregate_outcome = LensOutcome.FAILED
    elif LensOutcome.PENDING in outcomes:
        aggregate_outcome = LensOutcome.PENDING
    else:
        aggregate_outcome = LensOutcome.PASSED
    return _CodexCommentState(
        commit_sha=target_sha,
        outcome=aggregate_outcome,
        effective_at=comment.effective_at,
        comment_id=comment.comment_id,
    )


def _verify_user_id(
    actual: int | None,
    expected: int | None,
    bot_login: str,
    label: str,
) -> None:
    if expected is not None and actual != expected:
        raise InputError(f"{label} for {bot_login} has an unexpected author_id")


def _parse_reviews(raw: Any, source: str) -> tuple[PullRequestReviewRecord, ...]:
    items = _record_list(raw, "reviews", source)
    seen_ids: set[int] = set()
    parsed: list[PullRequestReviewRecord] = []
    for index, item in enumerate(items):
        label = f"reviews[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{label} must be an object in {source}")
        _reject_unknown(item, _REVIEW_KEYS, label, source)
        review_id = _positive_int(item.get("id"), f"{label}.id", source)
        if review_id in seen_ids:
            raise InputError(f"{label}.id is repeated in {source}")
        seen_ids.add(review_id)
        author_login = _login(item.get("author_login"), f"{label}.author_login", source)
        author_id = _optional_positive_int(item.get("author_id"), f"{label}.author_id", source)
        state = item.get("state")
        if not isinstance(state, str) or state not in _REVIEW_STATES:
            raise InputError(f"{label}.state is invalid in {source}")
        commit_id = _optional_sha(item.get("commit_id"), f"{label}.commit_id", source)
        submitted_at = _optional_timestamp(
            item.get("submitted_at"), f"{label}.submitted_at", source
        )
        body = _body(item.get("body", ""), f"{label}.body", source)
        parsed.append(
            PullRequestReviewRecord(
                review_id=review_id,
                author_login=author_login,
                author_id=author_id,
                state=state,
                commit_id=commit_id,
                submitted_at=submitted_at,
                body=body,
            )
        )
    return tuple(parsed)


def _parse_review_comments(raw: Any, source: str) -> tuple[PullRequestReviewCommentRecord, ...]:
    items = _record_list(raw, "review_comments", source)
    seen_ids: set[int] = set()
    parsed: list[PullRequestReviewCommentRecord] = []
    for index, item in enumerate(items):
        label = f"review_comments[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{label} must be an object in {source}")
        _reject_unknown(item, _REVIEW_COMMENT_KEYS, label, source)
        comment_id = _positive_int(item.get("id"), f"{label}.id", source)
        if comment_id in seen_ids:
            raise InputError(f"{label}.id is repeated in {source}")
        seen_ids.add(comment_id)
        review_id = _optional_positive_int(
            item.get("pull_request_review_id"), f"{label}.pull_request_review_id", source
        )
        author_login = _login(item.get("author_login"), f"{label}.author_login", source)
        author_id = _optional_positive_int(item.get("author_id"), f"{label}.author_id", source)
        commit_id = _optional_sha(item.get("commit_id"), f"{label}.commit_id", source)
        created_at = _timestamp(item.get("created_at"), f"{label}.created_at", source)
        path = _required_text(item.get("path"), f"{label}.path", source, maximum=MAX_PATH_BYTES)
        body = _body(item.get("body", ""), f"{label}.body", source)
        parsed.append(
            PullRequestReviewCommentRecord(
                comment_id=comment_id,
                pull_request_review_id=review_id,
                author_login=author_login,
                author_id=author_id,
                commit_id=commit_id,
                created_at=created_at,
                path=path,
                body=body,
            )
        )
    return tuple(parsed)


def _parse_issue_comments(raw: Any, source: str) -> tuple[PullRequestIssueCommentRecord, ...]:
    items = _record_list(raw, "issue_comments", source)
    seen_ids: set[int] = set()
    parsed: list[PullRequestIssueCommentRecord] = []
    for index, item in enumerate(items):
        label = f"issue_comments[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{label} must be an object in {source}")
        _reject_unknown(item, _ISSUE_COMMENT_KEYS, label, source)
        comment_id = _positive_int(item.get("id"), f"{label}.id", source)
        if comment_id in seen_ids:
            raise InputError(f"{label}.id is repeated in {source}")
        seen_ids.add(comment_id)
        author_login = _login(item.get("author_login"), f"{label}.author_login", source)
        author_id = _optional_positive_int(item.get("author_id"), f"{label}.author_id", source)
        app_id = _optional_positive_int(item.get("app_id"), f"{label}.app_id", source)
        created_at = _timestamp(item.get("created_at"), f"{label}.created_at", source)
        updated_at = _optional_timestamp(item.get("updated_at"), f"{label}.updated_at", source)
        body = _body(item.get("body"), f"{label}.body", source)
        parsed.append(
            PullRequestIssueCommentRecord(
                comment_id=comment_id,
                author_login=author_login,
                author_id=author_id,
                app_id=app_id,
                created_at=created_at,
                body=body,
                updated_at=updated_at,
            )
        )
    return tuple(parsed)


def _parse_reactions(raw: Any, source: str) -> tuple[PullRequestReactionRecord, ...]:
    items = _record_list(raw, "reactions", source)
    seen_ids: set[int] = set()
    parsed: list[PullRequestReactionRecord] = []
    for index, item in enumerate(items):
        label = f"reactions[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{label} must be an object in {source}")
        _reject_unknown(item, _REACTION_KEYS, label, source)
        reaction_id = _positive_int(item.get("id"), f"{label}.id", source)
        if reaction_id in seen_ids:
            raise InputError(f"{label}.id is repeated in {source}")
        seen_ids.add(reaction_id)
        author_login = _login(item.get("author_login"), f"{label}.author_login", source)
        author_id = _optional_positive_int(item.get("author_id"), f"{label}.author_id", source)
        content = item.get("content")
        if not isinstance(content, str) or content not in _REACTIONS:
            raise InputError(f"{label}.content is invalid in {source}")
        created_at = _timestamp(item.get("created_at"), f"{label}.created_at", source)
        parsed.append(
            PullRequestReactionRecord(
                reaction_id=reaction_id,
                author_login=author_login,
                author_id=author_id,
                content=content,
                created_at=created_at,
            )
        )
    return tuple(parsed)


def _parse_check_runs(raw: Any, source: str) -> tuple[PullRequestCheckRunRecord, ...]:
    items = _record_list(raw, "check_runs", source)
    seen_ids: set[int] = set()
    parsed: list[PullRequestCheckRunRecord] = []
    for index, item in enumerate(items):
        label = f"check_runs[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{label} must be an object in {source}")
        _reject_unknown(item, _CHECK_RUN_KEYS, label, source)
        check_run_id = _positive_int(item.get("id"), f"{label}.id", source)
        if check_run_id in seen_ids:
            raise InputError(f"{label}.id is repeated in {source}")
        seen_ids.add(check_run_id)
        name = _required_text(
            item.get("name"), f"{label}.name", source, maximum=MAX_CHECK_RUN_NAME_BYTES
        )
        if name.strip() != name or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
            raise InputError(f"{label}.name is invalid in {source}")
        status = item.get("status")
        if not isinstance(status, str) or status not in _CHECK_RUN_STATUSES:
            raise InputError(f"{label}.status is invalid in {source}")
        raw_conclusion = item.get("conclusion")
        if status == "completed":
            if not isinstance(raw_conclusion, str) or raw_conclusion not in _CHECK_RUN_CONCLUSIONS:
                raise InputError(f"{label}.conclusion is invalid in {source}")
            conclusion: str | None = raw_conclusion
        else:
            if raw_conclusion is not None:
                raise InputError(f"{label}.conclusion must be null when not completed in {source}")
            conclusion = None
        head_sha = _sha(item.get("head_sha"), f"{label}.head_sha", source)
        app_slug = _optional_text(
            item.get("app_slug"), f"{label}.app_slug", source, maximum=MAX_APP_SLUG_BYTES
        )
        app_id = _optional_positive_int(item.get("app_id"), f"{label}.app_id", source)
        started_at = _optional_timestamp(item.get("started_at"), f"{label}.started_at", source)
        completed_at = _optional_timestamp(
            item.get("completed_at"), f"{label}.completed_at", source
        )
        if started_at is not None and completed_at is not None and completed_at < started_at:
            raise InputError(f"{label}.completed_at precedes started_at in {source}")
        parsed.append(
            PullRequestCheckRunRecord(
                check_run_id=check_run_id,
                name=name,
                status=status,
                conclusion=conclusion,
                head_sha=head_sha,
                app_slug=app_slug,
                app_id=app_id,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
    return tuple(parsed)


def _select_latest_check_run(
    runs: list[PullRequestCheckRunRecord],
) -> PullRequestCheckRunRecord:
    def _pick_latest(items: list[PullRequestCheckRunRecord]) -> PullRequestCheckRunRecord:
        latest = items[0]
        for candidate in items[1:]:
            if (
                latest.started_at is not None
                and candidate.started_at is not None
                and candidate.started_at != latest.started_at
            ):
                if candidate.started_at > latest.started_at:
                    latest = candidate
            elif candidate.check_run_id > latest.check_run_id:
                latest = candidate
        return latest

    completed_runs = [item for item in runs if item.status == "completed"]
    incomplete_runs = [item for item in runs if item.status != "completed"]
    if not completed_runs:
        return _pick_latest(incomplete_runs)
    if not incomplete_runs:
        return _pick_latest(completed_runs)
    latest_completed = _pick_latest(completed_runs)
    latest_incomplete = _pick_latest(incomplete_runs)
    if latest_incomplete.check_run_id > latest_completed.check_run_id:
        return latest_incomplete
    completed_start = latest_completed.started_at or latest_completed.completed_at
    if (
        latest_incomplete.started_at is not None
        and completed_start is not None
        and latest_incomplete.started_at >= completed_start
    ):
        return latest_incomplete
    return latest_completed


def _record_list(value: Any, label: str, source: str) -> list[Any]:
    if not isinstance(value, list):
        raise InputError(f"pull-request review snapshot {label} must be an array in {source}")
    if len(value) >= MAX_SNAPSHOT_RECORDS:
        raise InputError(
            f"pull-request review snapshot {label} has {len(value)} records "
            f"(limit {MAX_SNAPSHOT_RECORDS - 1}); page is incomplete in {source}"
        )
    return value


def _positive_int(value: Any, label: str, source: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_PORTABLE_INT:
        raise InputError(f"pull-request review snapshot {label} is invalid in {source}")
    return value


def _optional_positive_int(value: Any, label: str, source: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, label, source)


def _sha(value: Any, label: str, source: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise InputError(f"pull-request review snapshot {label} must be a 40-char SHA in {source}")
    return value


def _optional_sha(value: Any, label: str, source: str) -> str | None:
    if value is None:
        return None
    return _sha(value, label, source)


def _optional_plan_digest(value: Any, source: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _PLAN_DIGEST.fullmatch(value) is None:
        raise InputError(f"pull-request review snapshot plan_digest is invalid in {source}")
    return value


def _login(value: Any, label: str, source: str) -> str:
    if not isinstance(value, str) or _LOGIN.fullmatch(value) is None:
        raise InputError(f"pull-request review snapshot {label} is invalid in {source}")
    return value


def _timestamp(value: Any, label: str, source: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise InputError(f"pull-request review snapshot {label} is invalid in {source}")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise InputError(
            f"pull-request review snapshot {label} is not a valid ISO-8601 timestamp in {source}"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InputError(
            f"pull-request review snapshot {label} must include a timezone offset in {source}"
        )
    return parsed.astimezone(UTC)


def _optional_timestamp(value: Any, label: str, source: str) -> datetime | None:
    if value is None:
        return None
    return _timestamp(value, label, source)


def _body(value: Any, label: str, source: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or "\x00" in value:
        raise InputError(f"pull-request review snapshot {label} is invalid in {source}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InputError(
            f"pull-request review snapshot {label} is not valid UTF-8 in {source}"
        ) from error
    if len(encoded) > MAX_SNAPSHOT_BODY_BYTES:
        raise InputError(
            f"pull-request review snapshot {label} exceeds {MAX_SNAPSHOT_BODY_BYTES} bytes in {source}"
        )
    return value


def _required_text(value: Any, label: str, source: str, *, maximum: int) -> str:
    text = _optional_text(value, label, source, maximum=maximum)
    if text is None:
        raise InputError(f"pull-request review snapshot {label} is required in {source}")
    return text


def _optional_text(value: Any, label: str, source: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise InputError(f"pull-request review snapshot {label} is invalid in {source}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InputError(
            f"pull-request review snapshot {label} is not valid UTF-8 in {source}"
        ) from error
    if len(encoded) > maximum:
        raise InputError(f"pull-request review snapshot {label} is oversized in {source}")
    return value


def _reject_unknown(
    mapping: dict[str, Any],
    allowed: frozenset[str],
    label: str,
    source: str,
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise InputError(f"{label} has unknown key {unknown[0]} in {source}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def is_review_actively_in_progress(
    evaluation: SnapshotEvaluation,
    *,
    aggregation: str = "all-required",
) -> bool:
    """Return True only when blocking pending lenses are actively running on head_sha."""
    if not isinstance(evaluation, SnapshotEvaluation):
        raise TypeError("evaluation must be a SnapshotEvaluation")
    if (
        evaluation.snapshot.draft
        or evaluation.aggregate.state is not LensOutcome.PENDING
        or not evaluation.aggregate.blocking_pending
    ):
        return False
    short_head = evaluation.snapshot.head_sha[:12]
    results_by_lens = {item.lens: item for item in evaluation.results.results}
    active_flags = [
        _is_lens_actively_in_progress(results_by_lens.get(name), short_head=short_head)
        for name in evaluation.aggregate.blocking_pending
    ]
    if aggregation == "any-required":
        return any(active_flags)
    return all(active_flags)


def _is_lens_actively_in_progress(result: LensResult | None, *, short_head: str) -> bool:
    if result is None or result.outcome is not LensOutcome.PENDING:
        return False
    summary = result.summary
    if summary is None or not summary.endswith(f"for head {short_head}"):
        return False
    return " review is in progress " in summary or summary.startswith("check-run ")
