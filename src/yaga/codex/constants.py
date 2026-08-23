"""Closed constants for the Codex review gate trust boundary."""

from __future__ import annotations

CODEX_CONNECTOR_USER_ID = 199_175_422
CODEX_CONNECTOR_LOGIN = "chatgpt-codex-connector[bot]"
CODEX_CONNECTOR_APP_ID = 1_144_995
CODEX_CONNECTOR_APP_SLUG = "chatgpt-codex-connector"
GITHUB_ACTIONS_USER_ID = 41_898_282
GITHUB_ACTIONS_LOGIN = "github-actions[bot]"

REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"})

STATUS_CONTEXT = "Codex Review"
DEFAULT_OBSERVER_WORKFLOW_PATH = ".github/workflows/review-policy-event.yml"
BOUNDARY_ACTIONS = frozenset({"edited", "opened", "ready_for_review", "reopened", "synchronize"})
OBSERVER_ACTIONS = BOUNDARY_ACTIONS | {"closed", "converted_to_draft"}
AUTOMATIC_REACTION_ACTIONS = frozenset({"opened"})
SOURCE_TITLE_KEYS = frozenset(
    {
        "action",
        "base",
        "base_ref",
        "base_changed",
        "boundary",
        "event",
        "head",
        "pr",
        "previous",
        "v",
    }
)

MAX_STATUS_DESCRIPTION_CHARS = 140
MAX_STATUSES_PER_CONTEXT = 1_000
MAX_SUCCESS_RACE_WRITES = 2
MAX_STATUSES_BEFORE_TERMINAL_WRITE = 100
MAX_BUFFERED_INVALIDATIONS = (
    MAX_STATUSES_PER_CONTEXT - (MAX_STATUSES_BEFORE_TERMINAL_WRITE - 1) - MAX_SUCCESS_RACE_WRITES
)
# Stop terminal writes once a head has 100 context entries. At the last allowed
# terminal write this leaves room for 899 raced invalidations, the stale success
# write, and one final pending repair. No finite reserve can bound a continuous
# trusted same-repository event stream, but pending may use the last API slot and
# remains fail-closed. Rotate the head after the terminal ceiling is reached.
# The REST client permits 300 requests for the complete publication. Validate
# only the semantically newest claimed boundaries and retain ample budget for
# ownership, evidence, status, and final race checks.
MAX_SOURCE_BOUNDARY_VALIDATIONS = 64
MAX_DISPLAY_TITLE_BYTES = 1_024
MAX_OUTCOME_BODY_BYTES = 256 * 1_024
MAX_OPEN_PULL_REQUESTS = 40
MAX_WORKFLOW_RUN_RECORDS = 100
MAX_STATUS_PAGE_RECORDS = 100
MAX_SCHEDULE_CANDIDATES = 4
SCHEDULE_INTERVAL_MINUTES = 30
MAX_SCHEDULE_RECONCILE_REQUESTS = 48
# Admit success only when the success request plus every bounded post-write
# validation and compensating pending request can still fit locally.
SUCCESS_WRITE_REQUEST_RESERVE = 9
SUCCESS_CLEANUP_MARGIN_SECONDS = 60
MIN_TERMINAL_JOB_TIMEOUT_MINUTES = 15
MAX_TERMINAL_JOB_TIMEOUT_MINUTES = 360

REACTION_CONTENTS = frozenset(
    {"+1", "-1", "confused", "eyes", "heart", "hooray", "laugh", "rocket"}
)
FORMAL_REVIEW_PREFIX = (
    "### 💡 Codex Review\n\nHere are some automated review suggestions for this pull request."
)
