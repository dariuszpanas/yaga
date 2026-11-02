"""Tests for pull-request review snapshot parsing and evaluation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yaga.agent_review.evaluation import LensOutcome
from yaga.agent_review.plan import build_plan
from yaga.agent_review.policy import (
    CODEX_DEFAULT_BOT_APP_ID,
    CODEX_DEFAULT_BOT_LOGIN,
    CODEX_DEFAULT_BOT_USER_ID,
    COPILOT_DEFAULT_BOT_LOGIN,
    AgentReviewPolicy,
    ReviewLens,
)
from yaga.agent_review.snapshot import (
    evaluate_pull_request_snapshot,
    load_pull_request_snapshot,
    parse_pull_request_snapshot,
)
from yaga.errors import ConfigurationError, InputError

HEAD_SHA = "a" * 40
OLD_SHA = "b" * 40
BASE_SHA = "c" * 40


def codex_and_copilot_policy() -> AgentReviewPolicy:
    return AgentReviewPolicy(
        version=1,
        required=("codex", "copilot"),
        aggregation="all-required",
        lenses=(
            ReviewLens("codex", "codex", "Review correctness.", "review"),
            ReviewLens("copilot", "copilot", "Review structure.", "review"),
        ),
    )


def single_codex_policy() -> AgentReviewPolicy:
    return AgentReviewPolicy(
        version=1,
        required=("codex",),
        aggregation="all-required",
        lenses=(ReviewLens("codex", "codex", "Review correctness.", "review"),),
    )


def base_snapshot_dict(
    *,
    policy: AgentReviewPolicy | None = None,
    **overrides: object,
) -> dict[str, object]:
    selected_policy = single_codex_policy() if policy is None else policy
    document: dict[str, object] = {
        "version": 1,
        "plan_digest": build_plan(selected_policy).digest(),
        "number": 42,
        "url": "https://github.com/dariuszpanas/yaga/pull/42",
        "state": "open",
        "draft": False,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "head_committed_at": "2026-11-01T18:00:00Z",
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }
    document.update(overrides)
    return document


def test_current_head_codex_thumbs_up_and_copilot_clean_review_pass(tmp_path: Path) -> None:
    policy = codex_and_copilot_policy()
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            base_snapshot_dict(
                policy=policy,
                reviews=[
                    {
                        "id": 10,
                        "author_login": COPILOT_DEFAULT_BOT_LOGIN,
                        "author_id": 500,
                        "state": "COMMENTED",
                        "commit_id": HEAD_SHA,
                        "submitted_at": "2026-11-01T18:05:00Z",
                        "body": "## Pull request overview\nLooks clean.",
                    }
                ],
                reactions=[
                    {
                        "id": 20,
                        "author_login": CODEX_DEFAULT_BOT_LOGIN,
                        "author_id": CODEX_DEFAULT_BOT_USER_ID,
                        "content": "+1",
                        "created_at": "2026-11-01T18:04:00Z",
                    }
                ],
            )
        ),
        encoding="utf-8",
    )

    snapshot = load_pull_request_snapshot(snapshot_path)
    evaluated = evaluate_pull_request_snapshot(policy, snapshot)

    assert evaluated.aggregate.state is LensOutcome.PASSED
    assert evaluated.results.outcomes() == {
        "codex": LensOutcome.PASSED,
        "copilot": LensOutcome.PASSED,
    }


def test_rejects_duplicate_bot_lenses_and_mismatched_plan_digest() -> None:
    duplicate_bot_policy = AgentReviewPolicy(
        version=1,
        required=("correctness", "security"),
        aggregation="all-required",
        lenses=(
            ReviewLens("correctness", "codex", "Review correctness.", "review"),
            ReviewLens("security", "codex", "Review security.", "review"),
        ),
    )
    snapshot = parse_pull_request_snapshot(base_snapshot_dict(policy=duplicate_bot_policy))
    with pytest.raises(ConfigurationError, match="distinct bot identities"):
        evaluate_pull_request_snapshot(duplicate_bot_policy, snapshot)

    other_policy = AgentReviewPolicy(
        version=1,
        required=("codex",),
        aggregation="all-required",
        lenses=(ReviewLens("codex", "codex", "Updated instruction.", "review"),),
    )
    single_snapshot = parse_pull_request_snapshot(base_snapshot_dict())
    with pytest.raises(ConfigurationError, match="plan_digest does not match"):
        evaluate_pull_request_snapshot(other_policy, single_snapshot)


def test_unbound_reactions_and_clean_comments_require_head_committed_at() -> None:
    snapshot = parse_pull_request_snapshot(
        base_snapshot_dict(
            head_committed_at=None,
            issue_comments=[
                {
                    "id": 15,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "app_id": CODEX_DEFAULT_BOT_APP_ID,
                    "created_at": "2026-11-01T18:04:00Z",
                    "body": "Codex Review: Didn't find any major issues.",
                }
            ],
            reactions=[
                {
                    "id": 20,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "content": "+1",
                    "created_at": "2026-11-01T18:04:00Z",
                }
            ],
        )
    )

    evaluated = evaluate_pull_request_snapshot(single_codex_policy(), snapshot)

    assert evaluated.aggregate.state is LensOutcome.PENDING
    assert "predates head" in (evaluated.results.results[0].summary or "")


def test_edited_or_stale_reviewed_commit_clean_comments_do_not_pass_new_head() -> None:
    policy = single_codex_policy()
    stale_footer_comment = parse_pull_request_snapshot(
        base_snapshot_dict(
            issue_comments=[
                {
                    "id": 16,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "app_id": CODEX_DEFAULT_BOT_APP_ID,
                    "created_at": "2026-11-01T18:02:00Z",
                    "body": (
                        "Codex Review: Didn't find any major issues.\n\n"
                        f"**Reviewed commit:** `{OLD_SHA[:10]}`"
                    ),
                }
            ]
        )
    )
    stale_eval = evaluate_pull_request_snapshot(policy, stale_footer_comment)
    assert stale_eval.aggregate.state is LensOutcome.PENDING
    assert f"is stale (commit {OLD_SHA[:10]})" in (stale_eval.results.results[0].summary or "")

    edited_clean_comment = parse_pull_request_snapshot(
        base_snapshot_dict(
            issue_comments=[
                {
                    "id": 17,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "app_id": CODEX_DEFAULT_BOT_APP_ID,
                    "created_at": "2026-11-01T17:00:00Z",
                    "updated_at": "2026-11-01T18:05:00Z",
                    "body": "Codex Review: Didn't find any major issues.",
                }
            ]
        )
    )
    with pytest.raises(InputError, match="was edited"):
        evaluate_pull_request_snapshot(policy, edited_clean_comment)


def test_pending_reaction_newer_than_formal_review_returns_pending() -> None:
    snapshot = parse_pull_request_snapshot(
        base_snapshot_dict(
            reviews=[
                {
                    "id": 10,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "state": "APPROVED",
                    "commit_id": HEAD_SHA,
                    "submitted_at": "2026-11-01T18:02:00Z",
                }
            ],
            reactions=[
                {
                    "id": 25,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "content": "eyes",
                    "created_at": "2026-11-01T18:05:00Z",
                }
            ],
        )
    )

    evaluated = evaluate_pull_request_snapshot(single_codex_policy(), snapshot)

    assert evaluated.aggregate.state is LensOutcome.PENDING
    assert "in progress (reaction: eyes)" in (evaluated.results.results[0].summary or "")


def test_codex_review_state_summary_comment_supports_pass_fail_pending_stale_and_resolved() -> None:
    policy = single_codex_policy()

    def summary_comment(
        *,
        comment_id: int = 99,
        commit_sha: str,
        status: str,
        has_any_findings: object,
        has_unresolved_findings: object,
        updated_at: str = "2026-11-01T18:05:00Z",
    ) -> dict[str, object]:
        marker = json.dumps(
            {
                "code_review": {
                    "commit_sha": commit_sha,
                    "has_any_findings": has_any_findings,
                    "has_unresolved_findings": has_unresolved_findings,
                    "status": status,
                },
                "security_review": {
                    "commit_sha": None,
                    "has_any_findings": None,
                    "has_unresolved_findings": None,
                    "status": None,
                },
            },
            separators=(",", ":"),
        )
        return {
            "id": comment_id,
            "author_login": CODEX_DEFAULT_BOT_LOGIN,
            "author_id": CODEX_DEFAULT_BOT_USER_ID,
            "app_id": CODEX_DEFAULT_BOT_APP_ID,
            "created_at": "2026-11-01T18:01:00Z",
            "updated_at": updated_at,
            "body": (
                "<!-- codex-pull-request-review-summary -->\n"
                f"<!-- codex-review-state:v1 {marker} -->\n\n"
                "## Codex Review Summary"
            ),
        }

    clean = parse_pull_request_snapshot(
        base_snapshot_dict(
            issue_comments=[
                summary_comment(
                    commit_sha=HEAD_SHA,
                    status="completed",
                    has_any_findings=False,
                    has_unresolved_findings=False,
                )
            ]
        )
    )
    assert evaluate_pull_request_snapshot(policy, clean).aggregate.state is LensOutcome.PASSED

    resolved_with_retained_inline = parse_pull_request_snapshot(
        base_snapshot_dict(
            reviews=[
                {
                    "id": 10,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "state": "COMMENTED",
                    "commit_id": HEAD_SHA,
                    "submitted_at": "2026-11-01T18:02:00Z",
                    "body": "### Codex Review",
                }
            ],
            review_comments=[
                {
                    "id": 100,
                    "pull_request_review_id": 10,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "commit_id": HEAD_SHA,
                    "created_at": "2026-11-01T18:02:00Z",
                    "path": "src/yaga/cli.py",
                    "body": "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow)</sub></sub> Finding**",
                }
            ],
            issue_comments=[
                summary_comment(
                    commit_sha=HEAD_SHA,
                    status="completed",
                    has_any_findings=True,
                    has_unresolved_findings=False,
                    updated_at="2026-11-01T18:05:00Z",
                )
            ],
        )
    )
    assert (
        evaluate_pull_request_snapshot(policy, resolved_with_retained_inline).aggregate.state
        is LensOutcome.PASSED
    )

    findings = parse_pull_request_snapshot(
        base_snapshot_dict(
            issue_comments=[
                summary_comment(
                    commit_sha=HEAD_SHA,
                    status="completed",
                    has_any_findings=True,
                    has_unresolved_findings=True,
                )
            ]
        )
    )
    assert evaluate_pull_request_snapshot(policy, findings).aggregate.state is LensOutcome.FAILED

    running = parse_pull_request_snapshot(
        base_snapshot_dict(
            issue_comments=[
                summary_comment(
                    commit_sha=HEAD_SHA,
                    status="in_progress",
                    has_any_findings=None,
                    has_unresolved_findings=None,
                )
            ]
        )
    )
    assert evaluate_pull_request_snapshot(policy, running).aggregate.state is LensOutcome.PENDING

    stale = parse_pull_request_snapshot(
        base_snapshot_dict(
            issue_comments=[
                summary_comment(
                    commit_sha=OLD_SHA,
                    status="completed",
                    has_any_findings=False,
                    has_unresolved_findings=False,
                    updated_at="2026-11-01T17:50:00Z",
                )
            ]
        )
    )
    stale_eval = evaluate_pull_request_snapshot(policy, stale)
    assert stale_eval.aggregate.state is LensOutcome.PENDING
    assert f"is stale (commit {OLD_SHA[:12]})" in (stale_eval.results.results[0].summary or "")

    completed_after_pending = parse_pull_request_snapshot(
        base_snapshot_dict(
            issue_comments=[
                summary_comment(
                    comment_id=98,
                    commit_sha=HEAD_SHA,
                    status="in_progress",
                    has_any_findings=None,
                    has_unresolved_findings=None,
                    updated_at="2026-11-01T18:02:00Z",
                ),
                summary_comment(
                    comment_id=99,
                    commit_sha=HEAD_SHA,
                    status="completed",
                    has_any_findings=False,
                    has_unresolved_findings=False,
                    updated_at="2026-11-01T18:05:00Z",
                ),
            ]
        )
    )
    assert (
        evaluate_pull_request_snapshot(policy, completed_after_pending).aggregate.state
        is LensOutcome.PASSED
    )

    pending_after_completed = parse_pull_request_snapshot(
        base_snapshot_dict(
            issue_comments=[
                summary_comment(
                    comment_id=98,
                    commit_sha=HEAD_SHA,
                    status="completed",
                    has_any_findings=False,
                    has_unresolved_findings=False,
                    updated_at="2026-11-01T18:02:00Z",
                ),
                summary_comment(
                    comment_id=99,
                    commit_sha=HEAD_SHA,
                    status="in_progress",
                    has_any_findings=None,
                    has_unresolved_findings=None,
                    updated_at="2026-11-01T18:05:00Z",
                ),
            ]
        )
    )
    assert (
        evaluate_pull_request_snapshot(policy, pending_after_completed).aggregate.state
        is LensOutcome.PENDING
    )

    for bad_any, bad_unresolved in (
        (False, "true"),
        ("true", False),
        (False, True),
    ):
        malformed = parse_pull_request_snapshot(
            base_snapshot_dict(
                issue_comments=[
                    summary_comment(
                        commit_sha=HEAD_SHA,
                        status="completed",
                        has_any_findings=bad_any,
                        has_unresolved_findings=bad_unresolved,
                    )
                ]
            )
        )
        with pytest.raises(InputError):
            evaluate_pull_request_snapshot(policy, malformed)


def test_stale_review_on_previous_commit_reports_pending_with_stale_summary() -> None:
    policy = codex_and_copilot_policy()
    snapshot = parse_pull_request_snapshot(
        base_snapshot_dict(
            policy=policy,
            reviews=[
                {
                    "id": 10,
                    "author_login": COPILOT_DEFAULT_BOT_LOGIN,
                    "state": "APPROVED",
                    "commit_id": OLD_SHA,
                    "submitted_at": "2026-11-01T17:50:00Z",
                }
            ],
            reactions=[
                {
                    "id": 20,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "content": "+1",
                    "created_at": "2026-11-01T17:55:00Z",
                }
            ],
        )
    )

    evaluated = evaluate_pull_request_snapshot(policy, snapshot)

    assert evaluated.aggregate.state is LensOutcome.PENDING
    by_lens = {item.lens: item for item in evaluated.results.results}
    assert by_lens["copilot"].outcome is LensOutcome.PENDING
    assert f"is stale (commit {OLD_SHA[:12]})" in (by_lens["copilot"].summary or "")
    assert by_lens["codex"].outcome is LensOutcome.PENDING
    assert "predates head" in (by_lens["codex"].summary or "")


def test_inline_findings_and_severity_badges_fail_current_head() -> None:
    policy = codex_and_copilot_policy()
    snapshot = parse_pull_request_snapshot(
        base_snapshot_dict(
            policy=policy,
            reviews=[
                {
                    "id": 10,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "state": "COMMENTED",
                    "commit_id": HEAD_SHA,
                    "submitted_at": "2026-11-01T18:05:00Z",
                    "body": "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange)</sub></sub> Missing bound**",
                },
                {
                    "id": 11,
                    "author_login": COPILOT_DEFAULT_BOT_LOGIN,
                    "state": "COMMENTED",
                    "commit_id": HEAD_SHA,
                    "submitted_at": "2026-11-01T18:06:00Z",
                    "body": "Found 1 issue.",
                },
            ],
            review_comments=[
                {
                    "id": 100,
                    "pull_request_review_id": 11,
                    "author_login": COPILOT_DEFAULT_BOT_LOGIN,
                    "commit_id": HEAD_SHA,
                    "created_at": "2026-11-01T18:06:00Z",
                    "path": "src/yaga/cli.py",
                    "body": "Check error exit code.",
                }
            ],
        )
    )

    evaluated = evaluate_pull_request_snapshot(policy, snapshot)

    assert evaluated.aggregate.state is LensOutcome.FAILED
    assert evaluated.aggregate.blocking_failures == ("codex", "copilot")


def test_eyes_reaction_after_head_reports_in_progress_pending() -> None:
    snapshot = parse_pull_request_snapshot(
        base_snapshot_dict(
            reactions=[
                {
                    "id": 30,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "content": "eyes",
                    "created_at": "2026-11-01T18:01:00Z",
                }
            ]
        )
    )

    evaluated = evaluate_pull_request_snapshot(single_codex_policy(), snapshot)

    assert evaluated.aggregate.state is LensOutcome.PENDING
    assert "in progress (reaction: eyes)" in (evaluated.results.results[0].summary or "")


def test_clean_codex_issue_comment_passes_and_non_clean_fails() -> None:
    policy = single_codex_policy()
    clean_snapshot = parse_pull_request_snapshot(
        base_snapshot_dict(
            issue_comments=[
                {
                    "id": 40,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "app_id": CODEX_DEFAULT_BOT_APP_ID,
                    "created_at": "2026-11-01T18:03:00Z",
                    "body": "Codex Review: Didn't find any major issues.",
                }
            ]
        )
    )
    assert (
        evaluate_pull_request_snapshot(policy, clean_snapshot).aggregate.state is LensOutcome.PASSED
    )

    failed_snapshot = parse_pull_request_snapshot(
        base_snapshot_dict(
            issue_comments=[
                {
                    "id": 41,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "app_id": CODEX_DEFAULT_BOT_APP_ID,
                    "created_at": "2026-11-01T18:03:00Z",
                    "body": "Unable to review this pull request due to usage limits.",
                }
            ]
        )
    )
    assert (
        evaluate_pull_request_snapshot(policy, failed_snapshot).aggregate.state
        is LensOutcome.FAILED
    )


def test_spoofed_author_id_or_app_id_fails_closed() -> None:
    policy = single_codex_policy()
    spoofed_user = parse_pull_request_snapshot(
        base_snapshot_dict(
            reactions=[
                {
                    "id": 50,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": 999,
                    "content": "+1",
                    "created_at": "2026-11-01T18:02:00Z",
                }
            ]
        )
    )
    with pytest.raises(InputError, match="unexpected author_id"):
        evaluate_pull_request_snapshot(policy, spoofed_user)

    spoofed_app = parse_pull_request_snapshot(
        base_snapshot_dict(
            issue_comments=[
                {
                    "id": 51,
                    "author_login": CODEX_DEFAULT_BOT_LOGIN,
                    "author_id": CODEX_DEFAULT_BOT_USER_ID,
                    "app_id": 888,
                    "created_at": "2026-11-01T18:02:00Z",
                    "body": "Codex Review: Didn't find any major issues.",
                }
            ]
        )
    )
    with pytest.raises(InputError, match="unexpected app_id"):
        evaluate_pull_request_snapshot(policy, spoofed_app)


def test_snapshot_rejects_truncated_hundred_record_page() -> None:
    reactions = [
        {
            "id": index + 1,
            "author_login": "octocat",
            "content": "+1",
            "created_at": "2026-11-01T18:02:00Z",
        }
        for index in range(100)
    ]
    with pytest.raises(InputError, match="page is incomplete"):
        parse_pull_request_snapshot(base_snapshot_dict(reactions=reactions))
