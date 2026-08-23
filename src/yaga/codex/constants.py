"""Closed constants for the Codex review gate trust boundary."""

from __future__ import annotations

CODEX_CONNECTOR_USER_ID = 199_175_422
CODEX_CONNECTOR_LOGIN = "chatgpt-codex-connector[bot]"
CODEX_CONNECTOR_APP_ID = 1_144_995
CODEX_CONNECTOR_APP_SLUG = "chatgpt-codex-connector"
GITHUB_ACTIONS_USER_ID = 41_898_282
GITHUB_ACTIONS_LOGIN = "github-actions[bot]"

REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"})
REACTION_CONTENTS = frozenset(
    {"+1", "-1", "confused", "eyes", "heart", "hooray", "laugh", "rocket"}
)

CODEX_STATUS_CONTEXT = "Codex Review"
CI_STATUS_CONTEXT = "CI Gate"
EVENT_ACTIONS = frozenset(
    {
        "closed",
        "converted_to_draft",
        "edited",
        "opened",
        "ready_for_review",
        "reopened",
        "synchronize",
    }
)
MAX_OUTCOME_BODY_BYTES = 256 * 1_024
MAX_PRIOR_REQUESTS = 8
MAX_STATUS_DESCRIPTION_CHARS = 140
MAX_STATUS_PAGE_RECORDS = 100
MAX_STATUSES_PER_CONTEXT = 1_000
LIFECYCLE_STATUS_SLOT_RESERVE = 8
MAX_HEAD_ASSOCIATIONS = 100
MAX_WORKFLOW_RUN_RECORDS = 100
MAX_INVALIDATION_REQUESTS = 16
MAX_AUTHORIZATION_REQUESTS = 128
MAX_PREPARATION_REQUESTS = 128
MAX_POLL_REQUESTS = 260
MAX_FINALIZATION_REQUESTS = 128
POLL_INTERVAL_SECONDS = 30
POLL_WINDOW_SECONDS = 8 * 60
LIFECYCLE_WAIT_SECONDS = 2 * 60
SUCCESS_WRITE_REQUEST_RESERVE = 48
ERROR_WRITE_REQUEST_RESERVE = 32
POLL_ITERATION_REQUEST_RESERVE = 80
SUCCESS_CLEANUP_MARGIN_SECONDS = 60
MIN_JOB_TIMEOUT_MINUTES = 15
MAX_JOB_TIMEOUT_MINUTES = 360

PENDING_DESCRIPTION = "Waiting for an exact-head Codex review outcome"
TIMEOUT_DESCRIPTION = "Codex review timed out; rerun CI after Codex finishes"
CI_FAILURE_DESCRIPTION = "CI prerequisites failed; Codex review was not requested"

FORMAL_REVIEW_PREFIX = (
    "### 💡 Codex Review\n\nHere are some automated review suggestions for this pull request."
)
